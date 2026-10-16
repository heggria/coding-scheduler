"""Read-only dashboard: never lock, reclaim, schedule, or write scheduler state."""
import json
import mimetypes
import re
import subprocess
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from affinity import model_for
import core
from core import TZ, counts, home, load_jobs, reason_text, remote_dir

TAIL_BYTES = 8192
LABEL = "com.heggria.cs-tick"
DIST = Path(__file__).with_name("dashboard_dist")
CACHE_SECONDS = 2
JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
LOG_ROUTE = re.compile(r"^/api/jobs/([^/]+)/log$")
LOG_NAMES = ("log", "stdout", "stderr", "conclusion.md")
LOG_SUFFIXES = (".log", ".out", ".err")
LOG_TAIL_DEFAULT, LOG_TAIL_MAX = 200, 2000
LOG_FILE_BYTES = 256 * 1024
LOG_MAX_FILES = 12


class BadRequest(Exception):
    pass


def parse_tail(query):
    values = parse_qs(query, keep_blank_values=True).get("tail")
    if values is None:
        return LOG_TAIL_DEFAULT
    if len(values) != 1 or not re.fullmatch(r"[0-9]{1,6}", values[0]):
        raise BadRequest("tail must be an integer")
    count = int(values[0])
    if not 1 <= count <= LOG_TAIL_MAX:
        raise BadRequest("tail out of range")
    return count


def tail_lines(path, count):
    """Last count lines of at most LOG_FILE_BYTES; drop a line cut by the byte cap."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return ""
    text = tail(path, LOG_FILE_BYTES)
    lines = text.splitlines()
    if size > LOG_FILE_BYTES and len(lines) > 1:
        lines = lines[1:]
    return "\n".join(lines[-count:])


def job_logs(jid, count):
    """Read-only log tails for one job; None when the job directory is absent."""
    if not JOB_ID.match(jid) or ".." in jid:
        raise BadRequest("bad job id")
    directory = remote_dir(jid)
    try:
        root = directory.parent.resolve()
        resolved = directory.resolve()
        if resolved.parent != root or not resolved.is_dir():
            return None
        names = sorted(item.name for item in resolved.iterdir()
                       if item.name in LOG_NAMES or item.name.endswith(LOG_SUFFIXES))
    except OSError:
        return None
    files = {}
    for name in names[:LOG_MAX_FILES]:
        try:
            target = (resolved / name).resolve()
        except OSError:
            continue
        if target.parent != resolved or not target.is_file():
            continue  # symlink escaping the job directory, or not a regular file
        files[name] = tail_lines(target, count)
    return {"id": jid, "files": files}


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
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed
    except (ValueError, TypeError):
        return None


def elapsed(job, now):
    start = timestamp(job.get("started_at"))
    end = timestamp(job.get("finished_at")) or now
    return round(max(0, (end - start).total_seconds()), 1) if start else None


def launchd_state():
    result = {"loaded": None, "running": None, "pid": None, "last_exit_status": None}
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=3)
        if proc.returncode:
            return result
        result.update(loaded=False, running=False)
        for line in proc.stdout.splitlines():
            fields = line.split()
            if len(fields) == 3 and fields[2] == LABEL:
                result.update(loaded=True, running=fields[0].isdigit(),
                              pid=int(fields[0]) if fields[0].isdigit() else None,
                              last_exit_status=int(fields[1]))
                break
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return result


def tick_state(root, now=None):
    now = now or datetime.now(TZ)
    service = launchd_state()
    latest = []
    for path in (root / "logs/tick.log", root / "logs/tick.err"):
        try:
            stat = path.stat()
            if stat.st_size:
                latest.append((stat.st_mtime, path))
        except OSError:
            pass
    last_at, error, source, age = None, None, None, None
    if latest:
        modified, source = max(latest, key=lambda item: item[0])
        last_at = datetime.fromtimestamp(modified, TZ).isoformat()
        age = round(max(0, now.timestamp() - modified), 1)
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
    # A periodic launchd job normally has no PID between invocations.
    healthy = service.get("loaded") is True and age is not None and age <= 180 and error is not True
    return {"last_at": last_at, "time_source": str(source) if source else None,
            "age_seconds": age, "healthy": healthy,
            "error": error, "launchd": service}


def snapshot(status_command=None):
    """Read atomic JSON files without invoking the mutating CLI status command.

    A transition can briefly appear in two folders; prefer its newer lifecycle.
    The server caches this best-effort snapshot for two seconds. No root creation.
    status_command is retained only for compatibility with bin/cs's call site.
    """
    root = home()
    caps = core.reload_caps(root)
    folders = {folder: load_jobs(root, folder) for folder in ("queue", "running", "done")}
    seen = set()
    for folder in ("done", "running", "queue"):
        unique = []
        for job in folders[folder]:
            if not isinstance(job, dict) or not isinstance(job.get("id"), str) or job["id"] in seen:
                continue
            unique.append(job)
            seen.add(job["id"])
        folders[folder] = unique
    now = datetime.now(TZ)
    running = []
    for job in folders["running"]:
        item = dict(job)
        lines = tail(job.get("log") or remote_dir(job["id"]) / "log").splitlines()
        item.update(elapsed_seconds=elapsed(job, now), log_last_line=lines[-1] if lines else "",
                    model=job.get("model") or model_for(job.get("pool"), job.get("tier"))[0])
        running.append(item)
    queued = []
    for job in folders["queue"]:
        item = dict(job)
        reasons = {json.dumps(reason, sort_keys=True, ensure_ascii=False): reason
                   for reason in job.get("queue_reasons", [])}
        item["queue_reasons"] = list(reasons.values())
        item["reason_text"] = reason_text(item["queue_reasons"])
        queued.append(item)
    completed = [dict(job, elapsed_seconds=elapsed(job, now)) for job in folders["done"]]
    completed.sort(key=lambda job: timestamp(job.get("finished_at")) or datetime.min.replace(tzinfo=TZ),
                   reverse=True)
    cutoff = now - timedelta(hours=24)
    recent = [job for job in completed if cutoff <= (timestamp(job.get("finished_at")) or
                                                   datetime.min.replace(tzinfo=TZ)) <= now]
    timeline = []
    for job in running + queued + completed:
        start = timestamp(job.get("created_at")) or timestamp(job.get("started_at"))
        finish = timestamp(job.get("finished_at")) or now
        if start and start <= now and finish >= cutoff:
            timeline.append(job)
    return {"schema_version": "1", "counts": counts(folders["running"]),
            "caps": caps, "reclaimed": 0,
            "running": folders["running"], "queued": folders["queue"],
            "dashboard": {"running": running, "queued": queued, "done": recent,
                          "completed": completed, "timeline": timeline,
                          "tick": tick_state(root, now), "updated_at": now.isoformat()}}


def make_server(host, port, status_command=None):
    cached = {"at": None, "body": None}

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
            try:
                parts = urlsplit(self.path)
                match = LOG_ROUTE.match(parts.path)
                if match or parts.path.startswith("/api/jobs/"):
                    try:
                        if not match:
                            self.respond(404, b"Not found", "text/plain; charset=utf-8")
                            return
                        count = parse_tail(parts.query)
                        result = job_logs(unquote(match.group(1)), count)
                    except BadRequest as error:
                        body = json.dumps({"error": str(error)}).encode("utf-8")
                        self.respond(400, body, "application/json; charset=utf-8")
                        return
                    if result is None:
                        self.respond(404, b'{"error":"job not found"}', "application/json; charset=utf-8")
                    else:
                        self.respond(200, json.dumps(result, ensure_ascii=False).encode("utf-8"),
                                     "application/json; charset=utf-8")
                    return
                path = unquote(parts.path)
                if path == "/api/state":
                    current = time.monotonic()
                    if cached["at"] is None or current - cached["at"] >= CACHE_SECONDS:
                        cached["body"] = json.dumps(snapshot(status_command), ensure_ascii=False).encode("utf-8")
                        cached["at"] = current
                    self.respond(200, cached["body"], "application/json; charset=utf-8")
                    return
                if "\x00" in path or "\\" in path or ".." in Path(path).parts:
                    self.respond(404, b"Not found", "text/plain; charset=utf-8")
                    return
                root = DIST.resolve()
                target = (root / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
                if root not in target.parents:
                    self.respond(404, b"Not found", "text/plain; charset=utf-8")
                elif target.is_file():
                    mime = {".js": "text/javascript", ".css": "text/css", ".html": "text/html",
                            ".svg": "image/svg+xml"}.get(target.suffix) or mimetypes.guess_type(str(target))[0]
                    mime = mime or "application/octet-stream"
                    if mime.startswith("text/"):
                        mime += "; charset=utf-8"
                    self.respond(200, target.read_bytes(), mime)
                else:
                    self.respond(404, b"Not found", "text/plain; charset=utf-8")
            except (OSError, ValueError):
                self.respond(503, b'{"error":"state unavailable"}', "application/json")

        do_HEAD = do_GET

        def method_not_allowed(self):
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def __getattr__(self, name):
            if name.startswith("do_"):
                return self.method_not_allowed
            raise AttributeError(name)

    return HTTPServer((host, port), Handler)


def serve(args, status_command=None):
    with make_server(args.host, args.port, status_command) as server:
        print("cs 看板：http://%s:%s" % server.server_address[:2], flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
