"""Worker-written conclusions survive the CLI's final-message output; signal kills are labelled."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core

# Fake CLIs: optionally write the conclusion path named in the prompt (as workers are told
# to), then emit a different final message the way each real CLI does.
FAKE_CODEX = r'''#!/usr/bin/python3
import os, re, sys
from pathlib import Path
args = sys.argv[1:]
out = Path(args[args.index("-o") + 1])
prompt = sys.stdin.read()
target = re.search(r"Write the final conclusion to (\S+)\. ", prompt).group(1)
if os.environ.get("FAKE_WRITE"):
    Path(target).write_text("full report\nNEED_FILES: a.py, b.py\nSTATUS: BLOCKED\n")
out.write_text("short summary [link](%s)\nSTATUS: %s\n" % (target, os.environ.get("FAKE_FINAL", "BLOCKED")))
raise SystemExit(int(os.environ.get("FAKE_RC", "0")))
'''
FAKE_CLAUDE = r'''#!/usr/bin/python3
import os, re, sys
from pathlib import Path
target = re.search(r"Write the final conclusion to (\S+)\. ", sys.argv[-1]).group(1)
if os.environ.get("FAKE_WRITE"):
    Path(target).write_text("a much longer worker written conclusion\nSTATUS: DONE\n")
print("final message\nSTATUS: DONE")
raise SystemExit(int(os.environ.get("FAKE_RC", "0")))
'''


class ConclusionOwnershipTests(unittest.TestCase):
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
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name, body in (("codex", FAKE_CODEX), ("claude", FAKE_CLAUDE)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)

    def launch(self, jid, pool):
        (self.base / "p.md").write_text("goal")
        job = dict(id=jid, cwd=str(self.cwd), files=[], prompt_file=str(self.base / "p.md"), tier="M",
                   created_at="2026-01-01T00:00:00+08:00")
        core.write_job(self.root, "queue", job)
        with patch.dict(os.environ, {"CS_TEST_FAKE_WORKER": "0"}), \
                patch.object(core, "git_output", return_value=None), \
                patch.object(core.subprocess, "Popen", return_value=Mock(pid=os.getpid())):
            core.launch(self.root, job, pool)
        return core.remote_dir(jid)

    def run_script(self, jd, **env):
        full = dict(os.environ, PATH=str(self.bin) + ":/usr/bin:/bin", **env)
        return subprocess.run(["/bin/bash", str(jd / "run.sh")], env=full, capture_output=True, timeout=20)

    def reclaim(self, jid):
        job = core.find_job(self.root, jid)
        job["pid"] = 2 ** 22 + 4321  # gone, so reclaim reads the exit file
        core.write_job(self.root, "running", job)
        core.reclaim(self.root)
        return core.find_job(self.root, jid)

    def test_codex_worker_written_conclusion_is_not_overwritten(self):
        jd = self.launch("codex-written", "codex")
        self.assertIn("-o %s" % (jd / "last-message.md"), (jd / "run.sh").read_text())
        self.assertEqual(self.run_script(jd, FAKE_WRITE="1").returncode, 0)
        self.assertIn("NEED_FILES: a.py, b.py", (jd / "conclusion.md").read_text())
        self.assertIn("short summary", (jd / "last-message.md").read_text())
        job = self.reclaim("codex-written")
        self.assertEqual(job["status"], "blocked")
        self.assertEqual(job["need_files"], ["a.py", "b.py"])
        summary = core.job_summary(job)
        self.assertEqual(summary["artifacts"]["last_message"], str(jd / "last-message.md"))

    def test_final_message_fills_missing_conclusion_and_exit_code_is_kept(self):
        jd = self.launch("codex-none", "codex")
        self.assertEqual(self.run_script(jd, FAKE_RC="3").returncode, 3)
        self.assertEqual((jd / "exit").read_text(), "3\n")
        self.assertIn("short summary", (jd / "conclusion.md").read_text())
        jd = self.launch("claude-none", "cc")
        self.assertEqual(self.run_script(jd).returncode, 0)
        self.assertEqual((jd / "conclusion.md").read_text(), "final message\nSTATUS: DONE\n")
        jd = self.launch("claude-written", "cc")
        self.assertEqual(self.run_script(jd, FAKE_WRITE="1").returncode, 0)
        # Old `> conclusion.md` truncated at start and clobbered the worker's file at exit.
        self.assertEqual((jd / "conclusion.md").read_text(),
                         "a much longer worker written conclusion\nSTATUS: DONE\n")

    def test_status_mismatch_between_conclusion_and_final_message_warns(self):
        jd = self.launch("mismatch", "codex")
        self.run_script(jd, FAKE_WRITE="1", FAKE_FINAL="DONE")
        job = self.reclaim("mismatch")
        self.assertEqual(job["status"], "blocked")  # the worker-written conclusion wins
        self.assertIn("conclusion_status_mismatch: conclusion.md=BLOCKED, final message=DONE", job["warnings"])

    def test_signal_kill_is_labelled_and_not_retried(self):
        for pool, jid in (("codex", "killed-codex"), ("opencode", "killed-open")):
            job = dict(id=jid, cwd=str(self.cwd), files=[], status="running", pool=pool,
                       created_at="2026-01-01T00:00:00+08:00", project="p", area="test", tier="M",
                       user_prompt="x")
            core.write_job(self.root, "running", job)
            jd = core.remote_dir(jid)
            jd.mkdir(parents=True)
            (jd / "exit").write_text("143\n")
            (jd / "log").write_text("Transport channel closed\n" if pool == "codex" else
                                    json.dumps({"type": "step_start"}) + "\n")
            core.reclaim(self.root)
            job = core.find_job(self.root, jid)
            with self.subTest(pool):
                self.assertEqual(job["status"], "failed")
                self.assertEqual(job["failure_reason"], "signaled")
                self.assertIn("SIGTERM", job["failure_detail"])
                self.assertNotIn("retried_as", job)
        self.assertEqual(core.load_jobs(self.root, "queue"), [])

    def test_reclassify_codex_labels_old_signal_kills(self):
        job = dict(id="old-kill", cwd=str(self.cwd), files=[], status="failed", pool="codex", exit=143,
                   finished_at=core.datetime.now(core.TZ).isoformat())
        core.write_job(self.root, "done", job)
        (core.remote_dir("old-kill")).mkdir(parents=True)
        changes = core.reclassify(self.root, "codex", since_s=3600, apply=True)
        self.assertEqual(changes[0]["after_reason"], "signaled")
        self.assertEqual(core.find_job(self.root, "old-kill")["failure_reason"], "signaled")

    def test_cc_credit_exhaustion_pauses_cc_pool_and_503_still_retries(self):
        for jid, body in (("cc-credit", "API Error: Request rejected (429) · murmurant: The member's "
                                        "monthly credit is used up or reserved by in-flight requests."),
                          ("cc-503", "API Error: 503 murmurant: No service resources are currently available")):
            job = dict(id=jid, cwd=str(self.cwd), files=[], status="running", pool="cc", project="p",
                       area="review", tier="M", user_prompt="x", created_at="2026-01-01T00:00:00+08:00")
            core.write_job(self.root, "running", job)
            jd = core.remote_dir(jid)
            jd.mkdir(parents=True)
            (jd / "exit").write_text("1\n")
            (jd / "last-message.md").write_text(body + "\n")
            (jd / "conclusion.md").write_text(body + "\n")
            core.reclaim(self.root)
        credit = core.find_job(self.root, "cc-credit")
        self.assertEqual(credit["failure_reason"], "rate_limit")
        self.assertNotIn("retried_as", credit)
        self.assertEqual(core.pool_pause(self.root, "cc")["job"], "cc-credit")
        self.assertEqual(core.find_job(self.root, "cc-503")["failure_reason"], "upstream_transient")
        self.assertIn("retried_as", core.find_job(self.root, "cc-503"))
        pinned = dict(id="pin-cc", cwd=str(self.cwd), files=[], pin="cc", tier="M", area="review")
        self.assertIsNone(core.check_queue(self.root, pinned, []))
        self.assertIn({"type": "pool_paused", "pool": "cc"},
                      [{k: r[k] for k in ("type", "pool")} for r in pinned["queue_reasons"]])
        free = dict(id="free", cwd=str(self.cwd), files=["a.py"], tier="M", area="backend")
        self.assertEqual(core.check_queue(self.root, free, []), "codex")
        core.clear_pool_pause(self.root, "cc")
        self.assertEqual(core.check_queue(self.root, dict(pinned), []), "cc")


if __name__ == "__main__":
    unittest.main()
