"""Isolated dashboard tests: no production state or real model workers."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import plistlib
import runpy
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
import dashboard

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_dashboard_tests")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        env = patch.dict(os.environ, {"HOME": str(self.base), "CS_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        core.ensure()
        self.now = datetime.now(core.TZ)
        clock = patch.object(core, "datetime", Mock(wraps=datetime))
        clock.start().now.return_value = self.now
        self.addCleanup(clock.stop)
        service = patch.object(dashboard, "launchd_state", return_value={
            "loaded": True, "running": False, "pid": None, "last_exit_status": 0})
        service.start()
        self.addCleanup(service.stop)
        self.server = dashboard.make_server("127.0.0.1", 0, CLI["cmd_status"])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def request(self, method, path):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            conn.request(method, path)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def record(self, jid, folder, **extra):
        job = dict(id=jid, project="fixture", cwd=str(self.base), tier="M", pool="codex",
                   status={"queue": "queued", "running": "running", "done": "done"}[folder])
        job.update(extra)
        core.write_job(self.root, folder, job)
        return job

    def test_http_page_api_and_status_contract(self):
        self.record("live", "running", pid=os.getpid(),
                    started_at=(self.now - timedelta(minutes=2)).isoformat())
        self.record("queued", "queue", queue_reasons=[{"type": "awaiting_tick"}])
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("实时看板", body.decode())
        self.assertIn(b"./assets/", body)
        status, headers, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)
        out = io.StringIO()
        with patch.object(sys, "argv", ["cs", "status", "--json"]), contextlib.redirect_stdout(out), \
                patch.dict(CLI["cmd_status"].__globals__, reclaim=Mock(return_value=0), write_board=Mock()):
            self.assertEqual(CLI["main"](), 0)
        expected = json.loads(out.getvalue())
        self.assertEqual({k: v for k, v in state.items() if k != "dashboard"}, expected)
        self.assertEqual(core.load_jobs(self.root, "queue")[0]["id"], "queued")
        self.assertEqual(state["dashboard"]["running"][0]["model"], "gpt-6.1-sol")
        self.assertGreaterEqual(state["dashboard"]["running"][0]["elapsed_seconds"], 120)

    def test_head_and_all_other_methods(self):
        for path in ("/", "/api/state"):
            status, headers, body = self.request("HEAD", path)
            self.assertEqual(status, 200)
            self.assertGreater(int(headers["Content-Length"]), 0)
            self.assertEqual(body, b"")
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "CUSTOM"):
            with self.subTest(method=method):
                status, headers, body = self.request(method, "/api/state")
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "GET, HEAD")
        self.assertEqual(self.request("GET", "/missing")[0], 404)

    def test_presentation_recent_jobs_dedup_and_bounded_log_tail(self):
        self.record("live", "running", pid=os.getpid(), model="custom-model")
        log = core.remote_dir("live") / "log"
        log.parent.mkdir(parents=True)
        log.write_bytes(b"x" * (dashboard.TAIL_BYTES * 4) + b"\nlast line\n")
        reasons = [{"type": "pool_full", "pool": "codex"}, {"type": "awaiting_tick"}]
        self.record("queued", "queue", queue_reasons=reasons + reasons)
        self.record("recent", "done", finished_at=self.now.isoformat(), exit=7, status="failed",
                    started_at=(self.now - timedelta(minutes=3)).isoformat())
        self.record("old", "done", finished_at=(self.now - timedelta(hours=25)).isoformat())
        self.record("legacy", "done")
        data = json.loads(self.request("GET", "/api/state")[2])["dashboard"]
        self.assertEqual(data["running"][0]["log_last_line"], "last line")
        self.assertEqual(data["running"][0]["model"], "custom-model")
        self.assertLessEqual(len(dashboard.tail(log)), dashboard.TAIL_BYTES)
        self.assertEqual(data["queued"][0]["queue_reasons"], reasons)
        self.assertIn("池满", data["queued"][0]["reason_text"])
        self.assertEqual([j["id"] for j in data["done"]], ["recent"])
        self.assertEqual(data["done"][0]["elapsed_seconds"], 180)
        self.assertEqual(data["done"][0]["exit"], 7)

    def test_snapshot_never_reclaims_locks_schedules_or_writes(self):
        self.record("dead", "running", pid=None)
        self.record("waiting", "queue")
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob("*") if p.is_file()}
        forbidden = Mock(side_effect=AssertionError("dashboard must be read-only"))
        with patch.object(core, "launch", forbidden), patch.object(core, "reclaim", forbidden), \
                patch.object(core, "state_lock", forbidden), patch.object(core, "ensure", forbidden), \
                patch.object(core, "write_board", forbidden), \
                patch.object(core, "write_job", forbidden):
            state = dashboard.snapshot(forbidden)
        forbidden.assert_not_called()
        self.assertEqual(state["reclaimed"], 0)
        self.assertEqual(state["running"][0]["id"], "dead")
        self.assertEqual(state["queued"][0]["id"], "waiting")
        self.assertEqual(state["dashboard"]["done"], [])
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_state_failure_returns_503(self):
        with patch.object(dashboard, "snapshot", side_effect=ValueError("broken")):
            self.assertEqual(self.request("GET", "/api/state")[0], 503)

    def test_tick_logs_report_error_success_and_missing(self):
        data = dashboard.tick_state(self.root)
        self.assertIsNone(data["last_at"])
        self.assertIsNone(data["error"])
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "tick.log").write_text('{"started": [], "queued": []}\n')
        self.assertFalse(dashboard.tick_state(self.root)["error"])
        (logs / "tick.err").write_text("Traceback: failure\n")
        os.utime(logs / "tick.err", (self.now.timestamp()+1, self.now.timestamp()+1))
        self.assertTrue(dashboard.tick_state(self.root)["error"])
        (logs / "tick.log").write_text('{"started": []}\n')
        os.utime(logs / "tick.log", (self.now.timestamp()+2, self.now.timestamp()+2))
        self.assertFalse(dashboard.tick_state(self.root)["error"])
        self.assertIsNotNone(dashboard.tick_state(self.root)["last_at"])

    def test_launchctl_readonly_and_unavailable(self):
        # Bypass the per-test stub to test the real parser with fake command output.
        with patch.object(dashboard, "launchd_state", service_parser):
            for pid, expected in (("123", True), ("-", False)):
                with patch.object(dashboard.subprocess, "run", return_value=Mock(
                        returncode=0, stdout="PID Status Label\n%s\t0\t%s\n" % (pid, dashboard.LABEL))) as run:
                    result = dashboard.launchd_state()
                    self.assertIs(result["running"], expected)
                    self.assertIs(result["loaded"], True)
                    self.assertEqual(run.call_args[0][0], ["launchctl", "list"])
            with patch.object(dashboard.subprocess, "run", side_effect=FileNotFoundError):
                self.assertIsNone(dashboard.launchd_state()["running"])
                self.assertIsNone(dashboard.launchd_state()["loaded"])
            with patch.object(dashboard.subprocess, "run", return_value=Mock(
                    returncode=0, stdout="- 0 com.heggria.cs-tick-other\n")):
                self.assertFalse(dashboard.launchd_state()["loaded"])

    def test_iso_utc_z_timestamps_on_python39(self):
        self.assertEqual(dashboard.timestamp("2026-10-16T08:00:00.000Z").timestamp(),
                         dashboard.timestamp("2026-10-16T16:00:00+08:00").timestamp())
        self.record("utc", "done", created_at="2026-10-16T07:59:00Z",
                    started_at="2026-10-16T08:00:00Z", finished_at="2026-10-16T08:05:00Z")
        self.assertEqual(dashboard.snapshot()["dashboard"]["completed"][0]["elapsed_seconds"], 300)

    def test_static_assets_mime_head_and_traversal(self):
        for asset in (dashboard.DIST / "assets").iterdir():
            path = "/assets/" + asset.name
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertEqual(body, asset.read_bytes())
            self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
            expected = {".js": "text/javascript", ".css": "text/css"}[asset.suffix]
            self.assertEqual(headers["Content-Type"], expected + "; charset=utf-8")
            self.assertEqual(self.request("HEAD", path)[2], b"")
        for path in ("/../dashboard.py", "/%2e%2e/dashboard.py", "/assets/%2e%2e/%2e%2e/core.py",
                     "/%2e%2e%2fcore.py", "/assets/", "/missing.js", "/assets%5c..%5ccore.py", "/%00"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)

    def test_static_symlink_escape_and_mime_fallback(self):
        dist = self.base / "dist"
        dist.mkdir()
        (dist / "leak.txt").symlink_to(REPO / "lib/core.py")
        (dist / "icon.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        (dist / "sample.bin").write_bytes(b"\x00\x01")
        with patch.object(dashboard, "DIST", dist):
            self.assertEqual(self.request("GET", "/leak.txt")[0], 404)
            self.assertEqual(self.request("GET", "/icon.svg")[1]["Content-Type"], "image/svg+xml")
            self.assertEqual(self.request("GET", "/sample.bin")[1]["Content-Type"],
                             "application/octet-stream")

    def test_tick_health_uses_loaded_not_running_and_three_minute_limit(self):
        logs = self.root / "logs"
        logs.mkdir()
        log = logs / "tick.log"
        log.write_text('{"started": []}\n')
        for loaded, age, healthy in ((True, 0, True), (True, 180, True),
                                     (True, 181, False), (False, 0, False), (None, 0, False)):
            with self.subTest(loaded=loaded, age=age):
                os.utime(log, (self.now.timestamp() - age, self.now.timestamp() - age))
                with patch.object(dashboard, "launchd_state", return_value={
                        "loaded": loaded, "running": False, "pid": None, "last_exit_status": 0}):
                    data = dashboard.tick_state(self.root, self.now)
                self.assertIs(data["healthy"], healthy)
                self.assertEqual(data["age_seconds"], age)
        log.unlink()
        self.assertFalse(dashboard.tick_state(self.root, self.now)["healthy"])

    def test_tick_fresh_errors_are_unhealthy(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "tick.log").write_text('{"error": "failed"}\n')
        self.assertFalse(dashboard.tick_state(self.root)["healthy"])
        (logs / "tick.log").write_text('{"started": []}\n')
        with patch.object(dashboard, "launchd_state", return_value={
                "loaded": True, "running": False, "pid": None, "last_exit_status": 1}):
            self.assertFalse(dashboard.tick_state(self.root)["healthy"])

    def test_snapshot_cache_expires_after_two_seconds(self):
        with patch.object(dashboard.time, "monotonic", side_effect=[100, 101.9, 102]), \
                patch.object(dashboard, "snapshot", wraps=dashboard.snapshot) as read:
            first = json.loads(self.request("GET", "/api/state")[2])
            self.record("later", "queue")
            self.assertEqual(json.loads(self.request("GET", "/api/state")[2]), first)
            fresh = json.loads(self.request("GET", "/api/state")[2])
            self.assertEqual(fresh["queued"][0]["id"], "later")
            self.assertEqual(read.call_count, 2)

    def test_lifecycle_includes_overlapping_jobs_and_all_completed(self):
        self.record("overlap", "done", created_at=(self.now - timedelta(hours=27)).isoformat(),
                    started_at=(self.now - timedelta(hours=26)).isoformat(),
                    finished_at=(self.now - timedelta(hours=23)).isoformat())
        self.record("old", "done", created_at=(self.now - timedelta(hours=27)).isoformat(),
                    finished_at=(self.now - timedelta(hours=25)).isoformat())
        self.record("queued", "queue", created_at=self.now.isoformat(),
                    queue_reasons=[{"type": "lock", "job": "overlap", "mine": "a.py",
                                    "theirs": "a.py", "kind": "shared"}])
        self.record("running", "running", started_at=self.now.isoformat())
        data = dashboard.snapshot()["dashboard"]
        self.assertEqual({job["id"] for job in data["timeline"]}, {"overlap", "queued", "running"})
        self.assertEqual({job["id"] for job in data["completed"]}, {"overlap", "old"})
        self.assertEqual(data["queued"][0]["queue_reasons"][0]["job"], "overlap")

    def test_missing_state_root_is_not_created(self):
        missing = self.base / "missing"
        with patch.dict(os.environ, CS_HOME=str(missing)):
            self.assertEqual(dashboard.snapshot()["running"], [])
        self.assertFalse(missing.exists())

    def test_duplicate_transition_prefers_terminal_record_and_skips_partial_json(self):
        self.record("transition", "queue")
        self.record("transition", "running")
        self.record("transition", "done", finished_at=self.now.isoformat())
        (self.root / "queue/broken.json").write_text("{")
        (self.root / "queue/invalid.json").write_text("[]")
        data = dashboard.snapshot()
        self.assertEqual(data["queued"], [])
        self.assertEqual(data["running"], [])
        self.assertEqual(data["dashboard"]["completed"][0]["id"], "transition")

    def test_cli_dispatch_does_not_hold_scheduler_lock(self):
        with patch.object(sys, "argv", ["cs", "dashboard", "--port", "0"]), \
                patch.object(dashboard, "serve", return_value=0) as serve, \
                patch.dict(CLI["main"].__globals__, state_lock=Mock(side_effect=AssertionError("lock held"))):
            self.assertEqual(CLI["main"](), 0)
            self.assertEqual(serve.call_args[0][0].port, 0)
            self.assertEqual(serve.call_args[0][0].host, "127.0.0.1")

    def test_launchd_definition(self):
        with (REPO / "launchd/com.heggria.cs-dashboard.plist").open("rb") as stream:
            config = plistlib.load(stream)
        self.assertEqual(config["Label"], "com.heggria.cs-dashboard")
        self.assertTrue(config["KeepAlive"])
        self.assertTrue(config["RunAtLoad"])
        self.assertEqual(config["ProgramArguments"][0], "/usr/bin/python3")
        self.assertEqual(config["ProgramArguments"][2:],
                         ["dashboard", "--host", "127.0.0.1", "--port", "8787"])
        self.assertEqual(config["StandardOutPath"], config["StandardErrorPath"])
        self.assertTrue(config["StandardOutPath"].endswith("/.coding-scheduler/dashboard.log"))


service_parser = dashboard.launchd_state

if __name__ == "__main__":
    unittest.main()
