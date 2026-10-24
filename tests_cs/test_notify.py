"""Local notification delivery contracts; no services or real workers."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
import notify

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_notify_cli")


class NotifyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "state"
        env = patch.dict(os.environ, {"CS_HOME": str(self.root), "CS_NOTIFY_CHANNEL": "off"})
        env.start()
        self.addCleanup(env.stop)

    def event(self, jid="one", status="done", at="2026-10-24T12:00:00+00:00", **fields):
        core.ensure(self.root)
        job = dict(id=jid, status=status, finished_at=at, pool="opencode", exit=0, **fields)
        core.write_job(self.root, "done", job)
        core.log_event(self.root, job)
        return job

    def drain(self, channel="terminal", output=None):
        with core.state_lock(self.root):
            return notify.drain(self.root, channel, output or (lambda text: None))

    def cli(self, *args, expected=0):
        output = io.StringIO()
        with patch.object(sys, "argv", ["cs", *args]), contextlib.redirect_stdout(output):
            code = CLI["main"]()
        self.assertEqual(code, expected, output.getvalue())
        return output.getvalue()

    def test_off_is_explicit_and_has_no_notification_state(self):
        self.assertFalse(notify.drain(self.root)["enabled"])
        self.assertFalse(self.root.exists())

    def test_duplicate_replay_rotation_and_status_transition(self):
        job = self.event()
        core.log_event(self.root, job)
        self.assertEqual(len(self.drain()["delivered"]), 1)
        events = self.root / "logs/events.jsonl"
        events.rename(events.with_name("events.jsonl.1"))
        core.log_event(self.root, job)
        self.assertEqual(self.drain()["delivered"], [])
        self.event(status="blocked")
        self.assertEqual(self.drain()["delivered"][0]["status"], "blocked")
        self.event(status="blocked", at="2026-10-24T13:00:00+00:00")
        self.assertEqual(len(self.drain()["delivered"]), 1)
        self.assertTrue((self.root / "notify/cursor.json").exists())

    def test_blocked_unknown_precede_success_and_failures(self):
        for jid, status in [("success", "done"), ("failure", "failed"),
                            ("blocked", "blocked"), ("unknown", "unknown")]:
            self.event(jid, status)
        delivered = self.drain()["delivered"]
        self.assertEqual([item["status"] for item in delivered],
                         ["blocked", "unknown", "failed", "done"])

    def test_failure_is_retryable_without_changing_job(self):
        job = self.event(status="unknown")
        path = self.root / "done/one.json"
        before = path.read_bytes()
        def fail(text):
            raise OSError("terminal unavailable")
        self.assertTrue(self.drain(output=fail)["errors"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.drain()["delivered"]), 1)
        ledger = json.loads((self.root / "notify/deliveries.json").read_text())
        self.assertEqual(next(iter(ledger.values()))["attempts"], 2)
        self.assertEqual(core.find_job(self.root, job["id"])["status"], "unknown")

    def test_inbox_is_idempotent_and_reuses_handoff(self):
        conclusion = self.root.parent / "conclusion.md"
        conclusion.write_text("Added local notifications.\nSTATUS: DONE\n")
        self.event(conclusion=str(conclusion))
        self.drain("inbox")
        # Simulate crash after writing inbox but before delivery acknowledgement.
        (self.root / "notify/deliveries.json").unlink()
        self.drain("inbox")
        files = list((self.root / "notify/inbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        message = json.loads(files[0].read_text())
        self.assertEqual(message["handoff"]["summary"], "Added local notifications.")
        self.assertIn("cs status one --no-reclaim", message["text"])
        self.assertIn(str(conclusion), message["text"])
        self.assertIn("从接手卡新开", message["next_step"])

    def test_resume_uses_shared_eligibility_and_never_executes(self):
        for pool, area, sid, supported in [("opencode", "backend", "sid", False),
                ("codex", "backend", None, False), ("cc", "review", "sid", False),
                ("codex", "backend", "sid", True), ("cc", "backend", "sid", True)]:
            with self.subTest(pool=pool, area=area, sid=sid):
                job = dict(id="one", status="done", pool=pool, area=area,
                           codex_session_id=sid, cc_session_id=sid)
                core.ensure(self.root)
                core.write_job(self.root, "done", job)
                message = notify.notification(self.root, dict(id="one", status="done"), "key")
                if supported:
                    self.assertIn("--resume-of one", message["next_step"])
                    self.assertIn("校验", message["next_step"])
                else:
                    self.assertIn("从接手卡新开", message["next_step"])
                self.assertEqual(core.load_jobs(self.root, "queue"), [])

    def test_invalid_events_and_partial_lines_are_ignored(self):
        self.event()
        with (self.root / "logs/events.jsonl").open("a") as stream:
            stream.write('\nnull\n[]\n{"id":"../bad","status":"done"}\n{"id":')
        self.assertEqual(len(self.drain()["delivered"]), 1)

    def test_cli_channel_json_and_no_reclaim(self):
        self.event()
        result = json.loads(self.cli("notify", "drain", "--json"))
        self.assertFalse(result["enabled"])
        with patch.object(core, "reclaim", side_effect=AssertionError("must not reclaim")):
            result = json.loads(self.cli("notify", "drain", "--channel", "inbox", "--json"))
        self.assertEqual(result["delivered"][0]["id"], "one")
        self.assertEqual(json.loads(self.cli("--json", "notify", "drain", "--channel", "inbox"))["delivered"], [])

    def test_submit_rejects_unsupported_resume_with_fallback(self):
        self.event()
        with patch.dict(os.environ, {"CS_JOB_ID": ""}):
            result = json.loads(self.cli("submit", "--project", "test", "--area", "backend",
                "--tier", "M", "--cwd", str(self.root), "--files", "x.py", "--prompt-text", "continue",
                "--resume-of", "one", "--no-tick", "--json", expected=2))
        self.assertIn("从接手卡新开", result["error"])

    def test_cli_inbox_failure_is_nonzero_and_retryable(self):
        self.event()
        directory = self.root / "notify"
        directory.mkdir()
        (directory / "inbox").write_text("not a directory")
        with contextlib.redirect_stderr(io.StringIO()):
            result = json.loads(self.cli("notify", "drain", "--channel", "inbox", "--json", expected=1))
        self.assertTrue(result["errors"])
        self.assertEqual(core.find_job(self.root, "one")["status"], "done")
        (directory / "inbox").unlink()
        with patch.dict(os.environ, {"CS_NOTIFY_CHANNEL": "inbox"}):
            self.assertEqual(len(json.loads(self.cli("notify", "drain", "--json"))["delivered"]), 1)

    def test_corrupt_delivery_state_fails_without_sending(self):
        self.event()
        directory = self.root / "notify"
        directory.mkdir()
        (directory / "deliveries.json").write_text('{"key": []}')
        with self.assertRaisesRegex(ValueError, "invalid notify"):
            self.drain(output=lambda text: self.fail("must not send"))


if __name__ == "__main__":
    unittest.main()
