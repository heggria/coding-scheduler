"""Read-only HTTP dashboard; status is the CLI's existing command function."""
import contextlib
import io
import json
import subprocess
from argparse import Namespace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from affinity import model_for
from core import TZ, ensure, home, load_jobs, reason_text, remote_dir, state_lock

TAIL_BYTES = 8192
LABEL = "com.heggria.cs-tick"


def tail(path, limit=TAIL_BYTES):
    """Read at most limit bytes, even for very large worker/tick logs."""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - limit))
            return stream.read(limit).decode("utf-8", errors="replace")
    except OSError:
        return ""


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def elapsed(job, now):
    start = timestamp(job.get("started_at"))
    end = timestamp(job.get("finished_at")) or now
    return round(max(0, (end - start).total_seconds()), 1) if start else None


def launchd_state():
    result = {"running": None, "pid": None, "last_exit_status": None}
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=3)
        if proc.returncode:
            return result
        result["running"] = False
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] == LABEL:
                result.update(running=fields[0].isdigit(),
                              pid=int(fields[0]) if fields[0].isdigit() else None,
                              last_exit_status=int(fields[1]))
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def tick_state(root):
    service = launchd_state()
    paths = [root / "logs/tick.log", root / "logs/tick.err"]
    latest = []
    for path in paths:
        try:
            if path.stat().st_size:
                latest.append((path.stat().st_mtime, path))
        except OSError:
            pass
    last_at, error = None, None
    source = None
    if latest:
        modified, source = max(latest, key=lambda item: item[0])
        last_at = datetime.fromtimestamp(modified, TZ).isoformat()
        if source.suffix == ".err":
            error = True
        else:
            lines = tail(source).splitlines()
            try:
                record = json.loads(lines[-1])
                error = bool(record.get("error")) if isinstance(record, dict) else None
            except (ValueError, IndexError):
                pass
    if service["last_exit_status"] not in (None, 0):
        error = True
    return {"last_at": last_at, "time_source": str(source) if source else None,
            "error": error, "launchd": service}


def snapshot(status_command):
    # Exactly the status --json path, including its allowed reclaim/board writes.
    # No scheduling, submission, cancellation, or model workers are invoked.
    with state_lock(ensure()):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = status_command(Namespace(id=None, lines=0, json=True))
        if code:
            raise ValueError("status failed")
        state = json.loads(output.getvalue())
        done = load_jobs(home(), "done")
    now = datetime.now(TZ)
    running = []
    for job in state["running"]:
        item = dict(job)
        lines = tail(remote_dir(job["id"]) / "log").splitlines()
        item.update(elapsed_seconds=elapsed(job, now), log_last_line=lines[-1] if lines else "",
                    model=job.get("model") or model_for(job.get("pool"), job.get("tier"))[0])
        running.append(item)
    queued = []
    for job in state["queued"]:
        item = dict(job)
        reasons = {json.dumps(reason, sort_keys=True, ensure_ascii=False): reason
                   for reason in job.get("queue_reasons", [])}
        item["queue_reasons"] = list(reasons.values())
        item["reason_text"] = reason_text(item["queue_reasons"])
        queued.append(item)
    recent = []
    for job in done:
        finished = timestamp(job.get("finished_at"))
        if finished and now - timedelta(hours=24) <= finished <= now:
            item = dict(job)
            item["elapsed_seconds"] = elapsed(job, now)
            recent.append(item)
    recent.sort(key=lambda job: timestamp(job["finished_at"]), reverse=True)
    # Preserve status fields verbatim; presentation data lives in dashboard.
    state["dashboard"] = {"running": running, "queued": queued, "done": recent,
                          "tick": tick_state(home()), "updated_at": now.isoformat()}
    return state


def make_server(host, port, status_command):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            try:
                if path == "/":
                    body = Path(__file__).with_name("dashboard.html").read_bytes()
                    self.respond(200, body, "text/html; charset=utf-8")
                elif path == "/api/state":
                    body = json.dumps(snapshot(status_command), ensure_ascii=False).encode("utf-8")
                    self.respond(200, body, "application/json; charset=utf-8")
                else:
                    self.respond(404, b"Not found", "text/plain")
            except (OSError, ValueError):
                self.respond(503, b'{"error":"state unavailable"}', "application/json")

        do_HEAD = do_GET

        def method_not_allowed(self):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        # BaseHTTPRequestHandler otherwise returns 501 for unknown methods.
        def __getattr__(self, name):
            if name.startswith("do_"):
                return self.method_not_allowed
            raise AttributeError(name)

    return HTTPServer((host, port), Handler)


def serve(args, status_command):
    with make_server(args.host, args.port, status_command) as server:
        print("cs 看板：http://%s:%s" % server.server_address[:2], flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
