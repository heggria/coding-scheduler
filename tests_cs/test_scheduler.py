"""A/D bot contracts; all state/worker artifacts are isolated, no model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
from affinity import pick_pool

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_cli")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "state"
        self.cwd = self.base / "work"
        self.cwd.mkdir()
        self.env = patch.dict(os.environ, {"CS_HOME": str(self.root), "CS_TEST_FAKE_WORKER": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)
        core.ensure()

    def cli(self, *args, stdin="", expected=0):
        out = io.StringIO()
        with patch.object(sys, "argv", ["cs", *args]), patch.object(sys, "stdin", io.StringIO(stdin)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = CLI["main"]()
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, expected, out.getvalue())
        return out.getvalue()

    def submit(self, jid="one", *extra, **kwargs):
        return json.loads(self.cli("submit", "--project", "fixture", "--area", "research", "--tier", "M",
                                   "--cwd", str(self.cwd), "--id", jid, "--json", *extra, **kwargs))

    def record(self, jid="one", folder="queue", **extra):
        job = dict(id=jid, cwd=str(self.cwd), files=[], status="queued" if folder == "queue" else folder,
                   created_at="2026-01-01T00:00:00+08:00")
        job.update(extra)
        core.write_job(self.root, folder, job)
        return job

    def test_home_alias_isolates_artifacts_and_old_environment_still_works(self):
        self.assertEqual(core.remote_dir("x"), self.root / "workers/x")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(core.home(), (Path.home() / ".coding-scheduler").resolve())
            self.assertEqual(core.remote_dir("x"), Path.home() / ".remote-jobs/x")
        with patch.dict(os.environ, {"CODING_SCHEDULER_HOME": str(self.root)}, clear=True):
            self.assertEqual(core.home(), self.root)
            self.assertEqual(core.remote_dir("x"), self.root / "workers/x")

    def test_fake_worker_refuses_production_root_before_writing(self):
        job = self.record()
        for env in ({"CS_TEST_FAKE_WORKER": "1"},
                    {"CS_TEST_FAKE_WORKER": "1", "CS_HOME": str(Path.home() / ".coding-scheduler")}):
            with patch.dict(os.environ, env, clear=True), patch.object(core, "remote_dir") as remote:
                with self.assertRaisesRegex(ValueError, "isolated"):
                    core.launch(self.root, job, "codex")
                remote.assert_not_called()

    def test_pool_choice_and_all_spill_reasons(self):
        self.assertEqual(pick_pool(90, 40, 1, 1), ("codex", []))
        self.assertEqual(pick_pool(90, 50, 0, 1), ("cc", []))
        pool, reasons = pick_pool(90, 40, 0, 0)
        self.assertIsNone(pool)
        self.assertEqual([r["type"] for r in reasons], ["pool_full", "pool_full", "spill_insufficient"])
        self.assertEqual(reasons[0]["primary"], "codex")
        self.assertEqual(reasons[0]["scores"]["cc"], 40)
        self.assertEqual(reasons[2]["minimum"], 45)
        self.assertEqual(pick_pool(30, 80, 1, 0)[1][-1]["type"], "spill_insufficient")

    def test_tick_all_lock_capacity_spill_and_quota_reasons(self):
        active = self.record("active", "running", status="running", pool="codex", pid=os.getpid(), files=["a.py"])
        active2 = self.record("active2", "running", status="running", pool="codex", pid=os.getpid(), files=["b.py"])
        self.record(files=["a.py", "b.py"], pin="codex")
        (self.root / "quota.json").write_text('{"codex": 0}')
        with patch.object(core, "CODEX_CAP", 2):
            result = core.tick(self.root)
        job = core.find_job(self.root, "one")
        self.assertEqual([r["type"] for r in job["queue_reasons"]],
                         ["lock", "lock", "pool_full", "spill_insufficient", "quota_exhausted"])
        self.assertEqual({r["job"] for r in job["blocked_by"]}, {active["id"], active2["id"]})
        self.assertEqual(job["queue_reasons"][0]["kind"], "write")
        self.assertEqual(result["queued"][0]["queue_reasons"], job["queue_reasons"])
        self.assertIn("+08:00", job["last_checked"])
        board = (self.root / "board.md").read_text()
        for text in ("原因", "占用", "池满", "分数", "额度耗尽"):
            self.assertIn(text, board)

    def test_quota_unknown_exhausted_positive_absent_and_malformed(self):
        for raw, expected in [(None, []), ('{"codex": null}', ["quota_unknown"]),
                              ('{"codex": 0}', ["quota_exhausted"]),
                              ('{"codex": 2}', []), ('{}', ["quota_unknown"]),
                              ('{"codex": true}', ["quota_unknown"]),
                              ('{"codex": -1}', ["quota_unknown"]),
                              ('{"codex": NaN}', ["quota_unknown"]),
                              ('[]', ["quota_unknown"]), ('broken', ["quota_unknown"])]:
            with self.subTest(raw=raw):
                path = self.root / "quota.json"
                if raw is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(raw)
                job = dict(id="probe", cwd=str(self.cwd), pin="codex")
                self.assertEqual(core.check_queue(self.root, job, []), "codex")
                self.assertEqual([r["type"] for r in job["queue_reasons"]], expected)
        (self.root / "quota.json").write_text('{"codex": null, "cc": 0}')
        self.assertEqual([r["type"] for r in core.quota_reasons(self.root, ["codex", "cc"])],
                         ["quota_unknown", "quota_exhausted"])

    def test_blocked_quota_then_start_clears_reasons_and_updates_check_time(self):
        self.record(pin="codex", prompt_file=str(self.root / "prompt"), blocked_by=[{"job": "old"}])
        (self.root / "prompt").write_text("task")
        (self.root / "quota.json").write_text('{"codex": null}')
        self.assertFalse(core.tick(self.root)["started"])
        first = core.find_job(self.root, "one")
        self.assertNotIn("blocked_by", first)
        (self.root / "quota.json").write_text('{"codex": 10}')
        self.assertEqual(core.tick(self.root)["started"], ["one"])
        running = core.find_job(self.root, "one")
        self.assertEqual(running["queue_reasons"], [])
        self.assertGreater(running["last_checked"], first["last_checked"])
        self.assertIsNone(running["base_sha"])
        self.assertEqual(json.loads(self.cli("wait", "one"))["jobs"][0]["status"], "done")

    def test_submit_text_stdin_file_and_templates(self):
        (self.root / "projects/fixture.md").write_text("project rules\n")
        first = self.submit("inline", "--prompt-text", "goal")
        self.assertEqual(first["schema_version"], "1")
        self.assertEqual((self.root / "queue/inline.prompt.md").read_text(), "project rules\n\ngoal")
        self.assertEqual(first["job"]["queue_reasons"][-1]["type"], "awaiting_tick")
        self.submit("stdin", "--prompt", "-", stdin="stdin goal")
        self.assertIn("stdin goal", (self.root / "queue/stdin.prompt.md").read_text())
        prompt = self.base / "prompt.md"
        prompt.write_text("file goal")
        self.submit("file", "--prompt", str(prompt))
        self.assertEqual((self.root / "queue/file.prompt.md").read_text(), "project rules\n\nfile goal")
        self.submit("off", "--prompt-text", "raw", "--no-template")
        self.assertEqual((self.root / "queue/off.prompt.md").read_text(), "raw")

    def test_prompt_sources_mutually_exclusive_and_required_json_errors(self):
        for extra in [(), ("--prompt-text", "x", "--prompt", "-")]:
            result = self.submit("bad", *extra, expected=2)
            self.assertEqual(result["schema_version"], "1")
            self.assertIn("error", result)
        result = self.submit("missing", "--prompt", str(self.base / "missing"), expected=2)
        self.assertIn("error", result)
        self.assertFalse((self.root / "queue/missing.json").exists())

    def test_submit_reports_blockers_immediately_with_or_without_tick(self):
        (self.root / "quota.json").write_text('{"codex": 0, "cc": 0}')
        for jid, flags in [("queued", []), ("tick", ["--tick"])]:
            result = self.submit(jid, "--prompt-text", "x", "--pin", "codex", *flags)
            self.assertIn("quota_exhausted", [r["type"] for r in result["job"]["queue_reasons"]])
        output = self.cli("submit", "--project", "p", "--area", "backend", "--tier", "L",
                          "--cwd", str(self.cwd), "--prompt-text", "x", "--tick")
        self.assertIn("额度耗尽", output)

    def test_idempotency_all_folders_and_no_prompt_overwrite(self):
        first = self.submit("original", "--prompt-text", "original", "--idempotency-key", "key")
        for folder in ("queue", "running", "done"):
            job = core.find_job(self.root, "original")
            core.remove_job(self.root, "queue", "original")
            core.remove_job(self.root, "running", "original")
            job["status"] = {"queue": "queued", "running": "running", "done": "done"}[folder]
            core.write_job(self.root, folder, job)
            again = self.submit("duplicate", "--prompt", "not-even-a-file", "--tick", "--idempotency-key", "key")
            self.assertTrue(again["existing"])
            self.assertEqual(again["id"], first["id"])
            self.assertIsNone(core.find_job(self.root, "duplicate"))
        self.assertEqual((self.root / "queue/original.prompt.md").read_text(), "original")

    def test_duplicate_id_rejected_and_generated_ids_do_not_collide(self):
        self.submit("same", "--prompt-text", "first")
        self.assertIn("error", self.submit("same", "--prompt-text", "second", expected=2))
        self.assertEqual((self.root / "queue/same.prompt.md").read_text(), "first")
        args = ("submit", "--project", "p", "--area", "test", "--tier", "L", "--cwd", str(self.cwd),
                "--prompt-text", "goal", "--json")
        self.assertNotEqual(json.loads(self.cli(*args))["id"], json.loads(self.cli(*args))["id"])

    def test_unknown_dead_worker_without_exit_and_invalid_exit(self):
        for jid, exit_text in [("absent", None), ("invalid", "garbage"), ("timeout", "124")]:
            self.record(jid, "running", status="running", pid=None)
            directory = core.remote_dir(jid)
            directory.mkdir(parents=True)
            if exit_text is not None:
                (directory / "exit").write_text(exit_text)
            (directory / "conclusion.txt").write_text("Looks completed; no reliable exit.")
        self.assertEqual(core.reclaim(self.root), 3)
        for job in core.load_jobs(self.root, "done"):
            self.assertEqual(job["status"], "unknown")
        self.assertEqual(core.find_job(self.root, "timeout")["outcome_reason"], "worker_timeout")
        self.assertIsNone(core.find_job(self.root, "absent")["exit"])

    def test_reclaim_live_legacy_job_and_confirmed_outcomes(self):
        self.record("live", "running", pid=os.getpid(), status="running")
        self.assertEqual(core.reclaim(self.root), 0)
        self.assertIn("last_checked", core.find_job(self.root, "live"))
        for jid, code, text, status in [("ok", 0, "Delivered", "done"),
                                       ("failed", 7, "Error", "failed"),
                                       ("blocked", 0, "未提交", "blocked"),
                                       ("empty", 0, "", "blocked")]:
            self.record(jid, "running", pid=None, status="running")
            jd = core.remote_dir(jid)
            jd.mkdir(parents=True)
            (jd / "exit").write_text(str(code))
            (jd / "conclusion.txt").write_text(text)
            core.reclaim(self.root)
            self.assertEqual(core.find_job(self.root, jid)["status"], status)

    def test_status_completed_summary_elapsed_artifacts_and_prs(self):
        conclusion = self.base / "conclusion.txt"
        conclusion.write_text("first\nsecond\nhttps://github.com/org/repo/pull/123\n" * 3)
        self.record("finished", "done", status="done", exit=0, conclusion=str(conclusion),
                    started_at="2026-01-01T00:00:00+08:00", finished_at="2026-01-01T00:00:05+08:00")
        data = json.loads(self.cli("status", "finished", "--json", "--lines", "2"))
        summary = data["job"]
        self.assertEqual(summary["status"], "done")
        self.assertEqual(summary["exit"], 0)
        self.assertEqual(summary["elapsed_seconds"], 5)
        self.assertEqual(summary["conclusion"], ["first", "second"])
        self.assertEqual(summary["pr_links"], ["https://github.com/org/repo/pull/123"])
        self.assertEqual(summary["artifacts"]["worktree"], str(self.cwd))
        self.assertEqual(summary["artifacts"]["conclusion"], str(conclusion))
        self.assertTrue(summary["artifacts"]["log"].endswith("/log"))
        self.assertNotIn("changed_files", summary)
        self.assertIn("done", self.cli("status", "finished"))
        self.assertIn("error", json.loads(self.cli("status", "missing", "--json", expected=1)))

    def test_wait_multiple_terminal_jobs_and_default_20_lines(self):
        conclusion = self.base / "conclusion.txt"
        conclusion.write_text("\n".join(str(n) for n in range(30)))
        for jid, status in [("one", "done"), ("two", "unknown"), ("three", "cancelled"), ("four", "failed")]:
            self.record(jid, "done", status=status, conclusion=str(conclusion))
        result = json.loads(self.cli("wait", "one", "two", "three", "four", "one", "--timeout", "0"))
        self.assertFalse(result["timed_out"])
        self.assertEqual(len(result["jobs"]), 4)
        self.assertEqual(len(result["jobs"][0]["conclusion"]), 20)
        result = json.loads(self.cli("wait", "one", "--lines", "0", "--json"))
        self.assertEqual(result["jobs"][0]["conclusion"], [])

    def test_wait_timeout_returns_current_state_and_does_not_mark_unknown(self):
        self.record("queued")
        self.record("active", "running", pid=os.getpid(), status="running")
        result = json.loads(self.cli("wait", "queued", "active", "--timeout", "0", expected=124))
        self.assertTrue(result["timed_out"])
        self.assertEqual([j["status"] for j in result["jobs"]], ["queued", "running"])
        self.assertEqual(core.find_job(self.root, "active")["status"], "running")
        self.assertIn("error", json.loads(self.cli("wait", "missing", expected=1)))

    def test_wait_blocks_and_reclaims_after_completion_signal_without_starting_workers(self):
        self.record("one", "running", status="running", pid=os.getpid())
        self.record("two", "running", status="running", pid=os.getpid())
        pending = ["one", "two"]

        def complete(_seconds):
            # A separate tick/cancel can acquire the state lock while wait sleeps.
            import fcntl
            with (self.root / ".lock").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            jd = core.remote_dir(pending.pop(0))
            jd.mkdir(parents=True)
            (jd / "exit").write_text("0")
            (jd / "conclusion.txt").write_text("Delivered")

        with patch("time.sleep", side_effect=complete) as sleep:
            result = json.loads(self.cli("wait", "one", "two", "--timeout", "1"))
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(result["timed_out"])
        self.assertEqual([j["status"] for j in result["jobs"]], ["done", "done"])

    def test_pr_links_scan_full_text_accept_bare_link_and_deduplicate(self):
        conclusion = self.base / "conclusion.txt"
        conclusion.write_text("summary\ngithub.com/org/repo/pull/9\nhttps://github.com/org/repo/pull/9\n"
                              "notgithub.com/org/repo/pull/8\n")
        job = self.record("pr", "done", status="done", conclusion=str(conclusion))
        summary = core.job_summary(job, lines=1)
        self.assertEqual(summary["conclusion"], ["summary"])
        self.assertEqual(summary["pr_links"], ["https://github.com/org/repo/pull/9"])

    def test_default_models_preserved_for_both_pools(self):
        for pool, model in [("codex", "gpt-6.1-sol"), ("cc", "murmurant/claude-haiku-5-5")]:
            self.submit(pool, "--prompt-text", "task")
            job = core.find_job(self.root, pool)
            job["tier"] = "L"
            core.launch(self.root, job, pool)
            self.assertEqual(core.find_job(self.root, pool)["model"], model)
            self.assertTrue((core.remote_dir(pool) / "prompt.md").read_text().startswith(core.NO_SUB))

    def test_wait_and_status_old_done_state_no_new_fields(self):
        self.record("old", "done", status="done")
        summary = json.loads(self.cli("status", "old", "--json"))["job"]
        self.assertIsNone(summary["elapsed_seconds"])
        self.assertIsNone(summary["exit"])
        self.assertEqual(summary["queue_reasons"], [])
        self.assertEqual(json.loads(self.cli("wait", "old"))["jobs"][0], summary)

    def test_git_summary_records_start_sha_and_includes_committed_and_uncommitted_changes(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.cwd), *args], check=True,
                                  capture_output=True, text=True, timeout=10).stdout.strip()
        git("init")
        git("config", "user.name", "Fixture")
        git("config", "user.email", "fixture@example.invalid")
        (self.cwd / "a.txt").write_text("base\n")
        git("add", ".")
        git("commit", "-m", "base")
        base_sha = git("rev-parse", "HEAD")
        self.submit("git", "--prompt-text", "goal", "--tick")
        self.assertEqual(core.find_job(self.root, "git")["base_sha"], base_sha)
        (self.cwd / "a.txt").write_text("committed\n")
        git("commit", "-am", "worker commit")
        (self.cwd / "a.txt").write_text("committed\nworking\n")
        summary = json.loads(self.cli("wait", "git"))["jobs"][0]
        self.assertEqual(summary["changed_files"], ["a.txt"])
        self.assertIn("a.txt", summary["diff_stat"])
        self.assertIn("2 insertions", summary["diff_stat"])

    def test_model_override_and_default_launch_arguments_safe_shell_and_failure_exit(self):
        self.submit("model", "--prompt-text", "goal", "--model", "custom-model", "--tick")
        self.assertEqual(core.find_job(self.root, "model")["model"], "custom-model")
        dangerous = "a model; $(touch BAD) `touch BAD2`"
        for pool in ("codex", "cc"):
            jid = "shell-" + pool
            job = self.record(jid, prompt_file=str(self.root / "queue/model.prompt.md"), model=dangerous)
            with patch.dict(os.environ, {"CS_TEST_FAKE_WORKER": "0"}), \
                    patch.object(core, "git_output", return_value=None), \
                    patch.object(core.subprocess, "Popen", return_value=Mock(pid=123)) as popen:
                core.launch(self.root, job, pool)
            script = core.remote_dir(jid) / "run.sh"
            self.assertIn(shlex.quote(dangerous), script.read_text())
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            fake_bin = self.base / ("fake-" + pool)
            fake_bin.mkdir()
            executable = fake_bin / ("codex" if pool == "codex" else "claude")
            executable.write_text("#!/bin/sh\nexit 7\n")
            executable.chmod(0o755)
            # Execute only the stub from the isolated PATH; no real model CLI.
            env = dict(os.environ, PATH=str(fake_bin) + ":/usr/bin:/bin")
            result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
            self.assertEqual((core.remote_dir(jid) / "exit").read_text().strip(), "7")
            self.assertFalse((self.cwd / "BAD").exists())
            self.assertFalse((self.cwd / "BAD2").exists())

    def test_tick_launch_failure_is_reported_not_started(self):
        self.record(prompt_file="missing")
        with patch.object(core, "launch", side_effect=OSError("fixture launch failure")):
            result = core.tick(self.root)
        self.assertEqual(result["started"], [])
        self.assertEqual(core.find_job(self.root, "one")["queue_reasons"][0]["type"], "launch_error")

    def test_every_command_json_schema_and_global_json_flag(self):
        self.submit("job", "--prompt-text", "goal")
        for args in [("status",), ("board",), ("cancel", "job")]:
            self.assertEqual(json.loads(self.cli(*args, "--json"))["schema_version"], "1")
        self.assertEqual(json.loads(self.cli("--json", "tick"))["schema_version"], "1")
        self.assertEqual(json.loads(self.cli("wait", "job", "--json"))["schema_version"], "1")
        checks = [{"name": "fixture", "ok": False, "level": "WARN", "detail": "no CLI"}]
        with patch.dict(CLI["main"].__globals__, doctor_checks=Mock(return_value=checks)):
            self.assertEqual(json.loads(self.cli("doctor", "--json"))["schema_version"], "1")
        for command in [("cancel", "missing"), ("status", "missing")]:
            self.assertEqual(json.loads(self.cli(*command, "--json", expected=1))["schema_version"], "1")

    def test_doctor_non_git_unrelated_cli_dns_and_trust_are_warn(self):
        with patch("shutil.which", return_value=None), patch("socket.getaddrinfo", side_effect=OSError("offline")):
            checks = core.doctor_checks(str(self.cwd), pool="codex")
        levels = {c["name"]: c["level"] for c in checks}
        self.assertEqual(levels["codex_cli"], "FAIL")
        for name in ("claude_cli", "github_dns", "git_repo"):
            self.assertEqual(levels[name], "WARN")
        with patch("shutil.which", return_value=None), patch("socket.getaddrinfo", return_value=[]):
            checks = core.doctor_checks(str(self.cwd))
        self.assertNotIn("FAIL", [c["level"] for c in checks])
        with patch.dict(CLI["main"].__globals__, doctor_checks=Mock(return_value=checks)):
            result = json.loads(self.cli("doctor", "--json"))
            self.assertIn("WARN", [c["level"] for c in result["checks"]])
        self.assertIn("FAIL", [c["level"] for c in core.doctor_checks(str(self.cwd / "missing"))])

    def test_invalid_identifiers_and_wait_limits_return_schema_errors(self):
        for args in [("status", "../bad"), ("wait", "one", "--timeout", "-1"),
                     ("wait", "one", "--timeout", "nan"), ("status", "one", "--lines", "-1")]:
            self.assertIn("error", json.loads(self.cli(*args, "--json", expected=2)))


if __name__ == "__main__":
    unittest.main()
