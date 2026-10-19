"""OpenCode outcome recovery, evidence-only failure reasons, CPU-aware idle, quota pause."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_cli")

INVALID = {"type": "error", "error": {"type": "provider.invalid-output",
                                      "message": "OpenAI Chat stream ended without finish_reason"}}
QUOTA = {"type": "error", "error": {"type": "provider.quota", "message": "Go usage limit exceeded",
                                    "status": 429}}


def text(value):
    return {"type": "text", "part": {"type": "text", "text": value}}


def finish(reason):
    return {"type": "step_finish", "part": {"type": "step-finish", "reason": reason}}


STEP, TOOL = {"type": "step_start"}, {"type": "tool_use", "part": {"tool": "bash"}}


class OpenCodeOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "state"
        self.cwd = self.base / "work"
        self.cwd.mkdir()
        env = patch.dict(os.environ, {"CS_HOME": str(self.root), "CS_TEST_FAKE_WORKER": "1"})
        os.environ.pop("CS_JOB_ID", None)
        env.start()
        self.addCleanup(env.stop)
        core.ensure()

    def cli(self, *args):
        out = io.StringIO()
        with patch.object(sys, "argv", ["cs", *args]), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(CLI["main"](), 0, out.getvalue())
        return out.getvalue()

    def worker(self, jid, events, exit_code=1, conclusion=None, log_extra="", folder="running", **extra):
        job = dict(id=jid, cwd=str(self.cwd), files=[], status="running", pool="opencode",
                   created_at="2026-01-01T00:00:00+08:00")
        job.update(extra)
        core.write_job(self.root, folder, job)
        jd = core.remote_dir(jid)
        jd.mkdir(parents=True, exist_ok=True)
        lines = "".join(json.dumps(event) + "\n" for event in events)
        (jd / "output.jsonl").write_text(lines)
        (jd / "log").write_text(lines + log_extra)
        if exit_code is not None:
            (jd / "exit").write_text("%s\n" % exit_code)
        if conclusion is not None:
            (jd / "conclusion.md").write_text(conclusion)
        return jd

    def reclaimed(self, jid):
        core.reclaim(self.root)
        return core.find_job(self.root, jid)

    def test_nonzero_exit_after_recovered_stream_errors_is_done_with_warning(self):
        cases = {
            "stop": [STEP, INVALID, STEP, TOOL, finish("tool-calls"), STEP, text("summary"), finish("stop")],
            "cut-final-text": [STEP, INVALID, STEP, text("wrote the conclusion")],
        }
        for name, events in cases.items():
            self.worker(name, events, conclusion="Delivered commit abc.\n\nSTATUS: DONE\n")
            job = self.reclaimed(name)
            with self.subTest(name):
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["exit"], 1)
                self.assertEqual(job["recovered_exit"], 1)
                self.assertTrue(any(w.startswith("opencode_nonzero_exit_recovered") for w in job["warnings"]))
                self.assertNotIn("failure_reason", job)
        self.worker("blocked", cases["stop"], conclusion="NEED_FILES: a.py\nSTATUS: BLOCKED\n")
        self.assertEqual(self.reclaimed("blocked")["status"], "blocked")

    def test_no_recovery_without_status_line_fatal_error_or_mid_turn_exit(self):
        cases = [
            ("no-status", [STEP, INVALID, STEP, text("done"), finish("stop")], "all good\n", "stream_truncated"),
            ("missing-conclusion", [STEP, INVALID, STEP, text("x"), finish("stop")], None, "stream_truncated"),
            ("quota", [STEP, INVALID, STEP, QUOTA], "STATUS: DONE\n", "rate_limit"),
            ("mid-step", [STEP, INVALID, STEP, TOOL, finish("tool-calls"), STEP], "STATUS: DONE\n", "abrupt_exit"),
            ("mid-tools", [STEP, TOOL, finish("tool-calls")], None, "abrupt_exit"),
            ("clean-exit-1", [STEP, text("x"), finish("stop")], "STATUS: DONE\n", None),
        ]
        for jid, events, conclusion, reason in cases:
            core.clear_pool_pause(self.root, "opencode")
            self.worker(jid, events, conclusion=conclusion)
            job = self.reclaimed(jid)
            with self.subTest(jid):
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job.get("failure_reason"), reason)
                self.assertNotIn("recovered_exit", job)

    def test_failure_reason_ignores_conclusion_and_task_prose(self):
        prose = [STEP, {"type": "tool_use", "part": {"output": "HTTP 401 forbidden rate-limit 429 401"}},
                 text("all other forbidden ports untouched; share-comment-rate-limit.test.ts passed"),
                 finish("tool-calls"), STEP]
        self.worker("prose", prose, conclusion="forbidden ports, HTTP 403, rate limit docs\nSTATUS: DONE\n")
        job = self.reclaimed("prose")
        self.assertEqual(job["failure_reason"], "abrupt_exit")
        # Real stderr lines and error events still classify.
        self.worker("stderr", [STEP], log_extra="Error: Unauthorized 401\n")
        self.assertEqual(self.reclaimed("stderr")["failure_reason"], "auth")
        self.worker("funds", [STEP, {"type": "error", "error": "Insufficient account funds"}])
        self.assertEqual(self.reclaimed("funds")["failure_reason"], "insufficient_funds")

    def test_run_script_extracts_conclusion_on_nonzero_exit_and_keeps_code(self):
        job = dict(id="shell", cwd=str(self.cwd), files=[], prompt_file=str(self.base / "p.md"), tier="M",
                   created_at="2026-01-01T00:00:00+08:00")
        (self.base / "p.md").write_text("goal")
        core.write_job(self.root, "queue", job)
        with patch.dict(os.environ, {"CS_TEST_FAKE_WORKER": "0"}), \
                patch.object(core, "git_output", return_value=None), \
                patch.object(core.subprocess, "Popen", return_value=Mock(pid=123)):
            core.launch(self.root, job, "opencode")
        jd = core.remote_dir("shell")
        fake = self.base / "bin"
        fake.mkdir()
        exe = fake / "opencode"
        exe.write_text("#!/usr/bin/python3\nimport json\n"
                       "print(json.dumps({'type':'error','error':{'type':'provider.invalid-output'}}))\n"
                       "print(json.dumps({'type':'text','part':{'text':'final\\nSTATUS: DONE\\n'}}))\n"
                       "raise SystemExit(1)\n")
        exe.chmod(0o755)
        env = dict(os.environ, PATH=str(fake) + ":/usr/bin:/bin")
        run = lambda: subprocess.run(["/bin/bash", str(jd / "run.sh")], env=env, capture_output=True, timeout=10)
        self.assertEqual(run().returncode, 1)
        self.assertEqual((jd / "exit").read_text(), "1\n")
        self.assertEqual((jd / "conclusion.md").read_text(), "final\nSTATUS: DONE\n")
        # A conclusion the worker wrote itself is not overwritten on a nonzero exit.
        (jd / "conclusion.md").write_text("worker written\nSTATUS: DONE\n")
        self.assertEqual(run().returncode, 1)
        self.assertEqual((jd / "conclusion.md").read_text(), "worker written\nSTATUS: DONE\n")
        exe.write_text("#!/bin/sh\nexit 7\n")
        (jd / "conclusion.md").unlink()
        self.assertEqual(run().returncode, 7)
        self.assertEqual((jd / "exit").read_text(), "7\n")

    def test_idle_counts_tree_cpu_activity_with_hard_cap(self):
        jd = core.remote_dir("busy")
        jd.mkdir(parents=True)
        (jd / "log").write_text("x")
        os.utime(jd / "log", (100, 100))
        job = dict(id="busy", pid=4242, started_at=core.datetime.fromtimestamp(100, core.TZ).isoformat())
        cpu = iter([10.0, 40.0, 70.0, 70.1, 70.2])
        with patch.object(core, "process_tree_cpu", side_effect=lambda pid: next(cpu)):
            self.assertFalse(core.opencode_idle(job, 600))   # baseline sample
            self.assertFalse(core.opencode_idle(job, 800))   # 30s CPU in 200s: busy tool
            self.assertFalse(core.opencode_idle(job, 1000))  # still busy
            self.assertFalse(core.opencode_idle(job, 1500))  # quiet, but < 600s since CPU activity
            self.assertTrue(core.opencode_idle(job, 1600))   # quiet for a full window
        job = dict(id="busy", pid=4242, started_at=core.datetime.fromtimestamp(100, core.TZ).isoformat())
        with patch.object(core, "process_tree_cpu", side_effect=lambda pid, n=iter(range(0, 10**6, 100)): float(next(n))), \
                patch.dict(os.environ, {"CS_OPENCODE_MAX_SILENT_S": "3000"}):
            for now in range(400, 3100, 300):
                self.assertFalse(core.opencode_idle(job, now), now)
            self.assertTrue(core.opencode_idle(job, 3100))   # hard cap since last output
        self.assertEqual(core.parse_cputime("1-02:03:04.50"), 86400 + 7384.5)
        self.assertEqual(core.parse_cputime("32:50.47"), 1970.47)
        self.assertIsNotNone(core.process_tree_cpu(os.getpid()))
        self.assertIsNone(core.process_tree_cpu(2 ** 22 + 12345))

    def test_quota_pauses_pool_routes_and_attributes_same_burst_exits(self):
        self.worker("early-abrupt", [STEP, TOOL, finish("tool-calls"), STEP], folder="running")
        core.reclaim(self.root)
        self.assertEqual(core.find_job(self.root, "early-abrupt")["failure_reason"], "abrupt_exit")
        self.worker("quota", [STEP, INVALID, STEP, QUOTA])
        self.worker("late-abrupt", [STEP, TOOL])
        core.reclaim(self.root)
        entry = core.pool_pause(self.root, "opencode")
        self.assertEqual(entry["job"], "quota")
        self.assertEqual(entry["strikes"], 1)
        for jid in ("quota", "early-abrupt", "late-abrupt"):
            self.assertEqual(core.find_job(self.root, jid)["failure_reason"], "rate_limit", jid)
        self.assertTrue(core.find_job(self.root, "early-abrupt")["quota_attributed"])
        pinned = dict(id="pinned", cwd=str(self.cwd), files=[], pin="opencode", tier="M")
        self.assertIsNone(core.check_queue(self.root, pinned, []))
        self.assertEqual(pinned["queue_reasons"][0]["type"], "pool_paused")
        self.assertIn("额度暂停至", core.reason_text(pinned["queue_reasons"]))
        light = dict(id="light", cwd=str(self.cwd), files=["a.py"], tier="L", area="test")
        self.assertIn(core.check_queue(self.root, light, []), ("codex", "cc"))
        status = json.loads(self.cli("status", "--json", "--no-reclaim"))
        self.assertEqual(status["paused"]["opencode"]["job"], "quota")
        # A second quota failure while paused does not escalate.
        again, armed = core.arm_pool_pause(self.root, "opencode", "other", "x")
        self.assertFalse(armed)
        self.assertEqual(again["strikes"], 1)
        # A failed probe after expiry doubles the pause, capped at 5h.
        until = core.timestamp(entry["until"]).timestamp()
        second, armed = core.arm_pool_pause(self.root, "opencode", "probe", "x", now=until + 60)
        self.assertTrue(armed)
        self.assertEqual(second["strikes"], 2)
        span = core.timestamp(second["until"]) - core.timestamp(second["since"])
        self.assertEqual(span.total_seconds(), 3600)
        self.assertIn("resumed", self.cli("pool", "--resume", "opencode"))
        self.assertIsNone(core.pool_pause(self.root, "opencode"))
        self.assertEqual(core.check_queue(self.root, dict(pinned), []), "opencode")

    def test_reclassify_dry_run_then_apply_updates_record_and_event_log(self):
        now = core.datetime.now(core.TZ).isoformat()
        common = dict(folder="done", status="failed", exit=1, finished_at=now)
        self.worker("old-recoverable", [STEP, INVALID, STEP, text("x"), finish("stop")],
                    conclusion="STATUS: DONE\n", failure_reason="auth", **common)
        self.worker("old-quota", [STEP, QUOTA], **common)
        self.worker("old-abrupt", [STEP, TOOL], failure_reason="opencode_error", **common)
        self.worker("old-idle", [STEP], failure_reason="no_output_timeout", **common)
        changes = {c["id"]: c for c in core.reclassify(self.root, since_s=3600)}
        self.assertEqual(changes["old-recoverable"]["after"], "done")
        self.assertEqual(changes["old-quota"]["after_reason"], "rate_limit")
        self.assertEqual(changes["old-abrupt"]["after_reason"], "rate_limit")  # same burst as quota
        self.assertNotIn("old-idle", changes)
        self.assertEqual(core.find_job(self.root, "old-recoverable")["status"], "failed")  # dry-run
        self.assertIsNone(core.pool_pause(self.root, "opencode"))  # replay never arms live pauses
        out = json.loads(self.cli("--json", "reclassify", "--apply", "--since", "1h"))
        self.assertTrue(out["applied"])
        job = core.find_job(self.root, "old-recoverable")
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["reclassified_from"]["status"], "failed")
        events = [json.loads(line) for line in (self.root / "logs/events.jsonl").read_text().splitlines()]
        self.assertIn("old-recoverable", {e["id"] for e in events if e["status"] == "done"})
        from stats import collect_stats
        self.assertEqual(collect_stats(self.root, "1h")["by_pool"]["opencode"]["total"], 4)
        self.assertEqual(core.reclassify(self.root, since_s=3600), [])  # idempotent


if __name__ == "__main__":
    unittest.main()
