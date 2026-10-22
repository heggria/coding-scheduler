"""A/D bot contracts; all state/worker artifacts are isolated, no model calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
from scope import policy_text
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
        os.environ.pop("CS_JOB_ID", None)
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
        # Generic fixtures use read-only research; explicit areas supply their own flags.
        if "--area" not in extra:
            extra = ("--read-only",) + extra
        return json.loads(self.cli("submit", "--project", "fixture", "--area", "research", "--tier", "M",
                                   "--cwd", str(self.cwd), "--id", jid, "--json", "--no-tick", *extra, **kwargs))

    def record(self, jid="one", folder="queue", **extra):
        job = dict(id=jid, cwd=str(self.cwd), files=[], status="queued" if folder == "queue" else folder,
                   created_at="2026-01-01T00:00:00+08:00")
        job.update(extra)
        core.write_job(self.root, folder, job)
        return job

    def test_stopped_descendant_is_paused_without_releasing_job(self):
        worker = subprocess.Popen([sys.executable, "-u", "-c",
            "import os,signal,sys; os.kill(os.getpid(),signal.SIGSTOP); "
            "print('resumed',flush=True); sys.stdin.read()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        def cleanup():
            if worker.poll() is None:
                os.kill(worker.pid, signal.SIGCONT)
                worker.terminate()
            worker.wait(timeout=5)
            worker.stdin.close()
            worker.stdout.close()
        self.addCleanup(cleanup)
        _, state = os.waitpid(worker.pid, os.WUNTRACED)
        self.assertTrue(os.WIFSTOPPED(state))
        job = self.record("paused-worker", "running", status="running", pool="codex",
                          pid=os.getpid(), files=["locked.py"])
        raw = (self.root / "running/paused-worker.json").read_bytes()
        data = json.loads(self.cli("status", "--no-reclaim", "--json"))
        self.assertEqual(data["running"][0]["status"], "paused")
        self.assertIn(worker.pid, data["running"][0]["paused_pids"])
        self.assertEqual(data["counts"]["codex"], 1)
        summary = json.loads(self.cli("status", job["id"], "--no-reclaim", "--json"))["job"]
        self.assertEqual(summary["status"], "paused")
        self.assertEqual((self.root / "running/paused-worker.json").read_bytes(), raw)
        self.assertEqual(core.reclaim(self.root), 0)
        self.assertEqual(core.load_jobs(self.root, "running")[0]["files"], ["locked.py"])
        os.kill(worker.pid, signal.SIGCONT)
        self.assertEqual(worker.stdout.readline().strip(), "resumed")
        self.assertEqual(core.observe_running([job])[0]["status"], "running")

    def test_process_probe_failure_and_invalid_pid_do_not_invent_pause(self):
        jobs = [dict(id=str(pid), pid=pid, status="running") for pid in (None, -1, 0)]
        with patch.object(core.subprocess, "run", side_effect=subprocess.TimeoutExpired("ps", 5)):
            self.assertEqual(core.observe_running(jobs), jobs)
        with patch.object(core, "process_snapshot", return_value={7: (1, "Ts")}):
            self.assertEqual(core.observe_running(jobs), jobs)
            self.assertEqual(core.observe_running([dict(pid=7, status="running")])[0]["status"], "paused")

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

    def test_terminal_transitions_append_one_event_each(self):
        for jid, exit_code, conclusion, expected in (
                ("ok", "0", "STATUS: DONE", "done"),
                ("block", "0", "STATUS: BLOCKED\n清单外文件", "blocked"),
                ("fail", "1", "STATUS: DONE", "failed"),
                ("unknown", "124", "STATUS: DONE", "unknown")):
            self.submit(jid, "--prompt-text", "job " + jid, "--tick")
            worker = core.remote_dir(jid)
            (worker / "exit").write_text(exit_code)
            (worker / "conclusion.md").write_text(conclusion)
            core.reclaim(self.root)
            self.assertEqual(core.find_job(self.root, jid)["status"], expected)
        self.submit("cancel", "--prompt-text", "中文 cancel", "--tick")
        (core.remote_dir("cancel") / "conclusion.md").write_text(" STATUS: DONE \n")
        self.cli("cancel", "cancel", "--json")
        events_path = self.root / "logs/events.jsonl"
        before = events_path.read_bytes()
        self.assertEqual(core.reclaim(self.root), 0)
        self.cli("cancel", "cancel", "--json", expected=1)
        self.assertEqual(events_path.read_bytes(), before)
        events = [json.loads(line) for line in before.splitlines()]
        self.assertEqual([e["status"] for e in events], ["done", "blocked", "failed", "unknown", "cancelled"])
        self.assertEqual(events[1]["blocked_reason_class"], "files_narrow")
        self.assertTrue(events[-1]["cancelled_after_done"])
        for event in events:
            self.assertGreater(event["prompt_bytes"], 0)
            self.assertIsNotNone(event["elapsed_s"])
            self.assertEqual(event["project"], "fixture")

    def test_queued_cancel_event_preserves_reason_observation(self):
        self.submit("cancel-queued", "--prompt-text", "cancel")
        self.cli("cancel", "cancel-queued", "--json")
        event = json.loads((self.root / "logs/events.jsonl").read_text())
        self.assertEqual(event["queue_reason_types"], ["awaiting_tick"])
        self.assertIsNone(event["elapsed_s"])
        self.assertFalse(event["cancelled_after_done"])

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

    def test_caps_default_file_env_and_cc_unchanged(self):
        import affinity
        with patch.dict(os.environ):
            os.environ.pop("CS_CODEX_CAP", None)
            os.environ.pop("CS_CC_CAP", None)
            os.environ.pop("CS_OPENCODE_CAP", None)
            self.assertEqual(core.reload_caps(), {"codex": 24, "cc": 6, "opencode": 32})
            self.assertEqual((affinity.CODEX_CAP, affinity.CC_CAP), (24, 6))
            os.environ["CS_CODEX_CAP"] = "30"
            os.environ["CS_CC_CAP"] = "bad"
            self.assertEqual(core.reload_caps(), {"codex": 30, "cc": 6, "opencode": 32})
            (self.root / "caps.json").write_text('{"codex": 8, "cc": 3}')
            self.assertEqual(core.reload_caps(), {"codex": 8, "cc": 3, "opencode": 32})
            self.assertEqual((core.CODEX_CAP, core.CC_CAP), (8, 3))
            (self.root / "caps.json").write_text('{"codex": 0, "cc": true}')
            self.assertEqual(core.reload_caps(), {"codex": 30, "cc": 6, "opencode": 32})
            (self.root / "caps.json").write_text("not json")
            self.assertEqual(core.reload_caps(), {"codex": 30, "cc": 6, "opencode": 32})
            (self.root / "caps.json").unlink()
            os.environ.pop("CS_CODEX_CAP")
            self.assertEqual(core.reload_caps(), {"codex": 24, "cc": 6, "opencode": 32})

    def test_import_defaults_and_status_picks_up_caps_json(self):
        import affinity
        self.assertEqual(affinity.DEFAULT_CAPS, {"codex": 24, "cc": 6, "opencode": 32})
        (self.root / "caps.json").write_text('{"codex": 5}')
        out = json.loads(self.cli("status", "--json", "--no-reclaim"))
        self.assertEqual(out["caps"], {"codex": 5, "cc": 6, "opencode": 32})
        (self.root / "caps.json").write_text('{"codex": 7, "cc": 2}')
        out = json.loads(self.cli("status", "--json", "--no-reclaim"))
        self.assertEqual(out["caps"], {"codex": 7, "cc": 2, "opencode": 32})
        core.write_board(self.root)
        self.assertIn("Codex `0/7` · CC `0/2`", (self.root / "board.md").read_text())

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

    def test_opencode_caps_env_file_reload_counts_and_visibility(self):
        import affinity
        with patch.dict(os.environ, {"CS_OPENCODE_CAP": "9"}):
            self.assertEqual(core.reload_caps()["opencode"], 9)
            self.assertEqual(affinity.OPENCODE_CAP, 9)
            (self.root / "caps.json").write_text('{"opencode": 2}')
            self.assertEqual(core.reload_caps()["opencode"], 2)
            (self.root / "caps.json").write_text('{"opencode": null}')
            self.assertEqual(core.reload_caps()["opencode"], 9)
            os.environ["CS_OPENCODE_CAP"] = "0"
            self.assertEqual(core.reload_caps()["opencode"], 32)
        for raw in ('{"opencode": true}', '{"opencode": -1}', '{"opencode": "bad"}'):
            (self.root / "caps.json").write_text(raw)
            with patch.dict(os.environ, {"CS_OPENCODE_CAP": "invalid"}):
                self.assertEqual(affinity.load_caps(self.root)["opencode"], 32)
        (self.root / "caps.json").write_text('{"opencode": 3}')
        self.record("active", folder="running", pool="opencode", pid=os.getpid())
        self.assertEqual(core.counts(core.load_jobs(self.root, "running"))["opencode"], 1)
        self.assertIn("OpenCode 1/3", self.cli("status", "--no-reclaim"))
        core.write_board(self.root)
        self.assertIn("OpenCode `1/3`", (self.root / "board.md").read_text())
        import dashboard
        self.assertEqual(dashboard.snapshot()["caps"]["opencode"], 3)

    def test_opencode_routing_matrix_and_no_spill(self):
        (self.root / "caps.json").write_text('{"codex": 2, "cc": 2, "opencode": 2}')
        base = dict(id="probe", cwd=str(self.cwd), tier="L", area="backend",
                    files=["src/a.py"], risk="low", shared_files=[])
        open_full = [dict(id="open-%s" % i, pool="opencode", files=[]) for i in range(2)]
        paid_full = [dict(id="%s-%s" % (p, i), pool=p, files=[])
                     for p in ("codex", "cc") for i in range(2)]
        cases = [({}, [], "opencode"), ({}, open_full, "codex"),
                 ({"pin": "opencode", "tier": "H"}, [], "opencode"),
                 ({"pin": "opencode"}, open_full, None),
                 ({"pin": "codex"}, [], "codex"), ({"pin": "cc"}, [], "cc"),
                 ({"pin": "codex"}, paid_full, None), ({"pin": "cc"}, paid_full, None),
                 ({"tier": "M"}, [], "codex"), ({"tier": "H"}, paid_full, None),
                 ({"risk": "high"}, [], "codex"),
                 ({"shared_files": ["src/shared.py"]}, [], "codex"),
                 ({"files": ["a", "b", "c", "d"]}, [], "codex"),
                 ({"files": ["a", "b", "c"]}, [], "opencode"),
                 ({"area": "research", "read_only": True, "risk": "high",
                   "files": ["lib/core.py"], "shared_files": ["shared"]}, [], "opencode")]
        for changes, running, expected in cases:
            job = dict(base, **changes)
            with self.subTest(changes=changes, pools=[j["pool"] for j in running]):
                self.assertEqual(core.check_queue(self.root, job, running), expected)
                if changes.get("pin") == "opencode" and running:
                    self.assertEqual(job["queue_reasons"], [
                        {"type": "pool_full", "pool": "opencode", "free": 0}])
        # Pins use ordinary affinity scores only for diagnostics, never to pick paid pools.
        import affinity
        self.assertEqual(affinity.score(dict(base, pin="opencode"), {}, {}),
                         affinity.score(base, {}, {}))
        blocker = dict(id="locked", cwd=str(self.cwd), files=["src/a.py"], pool="cc")
        job = dict(base)
        self.assertEqual(core.check_queue(self.root, job, [blocker]), "opencode")
        self.assertEqual([r["type"] for r in job["queue_reasons"]], ["lock"])

    def test_opencode_high_risk_paths_normalized_relative_to_repo(self):
        subprocess.run(["git", "init", str(self.cwd)], check=True, capture_output=True)
        (self.cwd / "src").mkdir()
        for path in ("lib/core.py", "./lib/affinity.py", "src/../bin/cs", "launchd/agent.plist",
                     str(self.cwd / "lib/core.py"), "../lib/core.py"):
            cwd = self.cwd / "src" if path == "../lib/core.py" else self.cwd
            job = dict(id="probe", tier="L", area="backend", cwd=str(cwd), files=[path])
            with self.subTest(path=path):
                self.assertFalse(core.low_risk(job))
                self.assertNotEqual(core.check_queue(self.root, job, []), "opencode")
        self.assertTrue(core.low_risk(dict(cwd=str(self.cwd), files=["src/lib/core.py"])))

    def test_opencode_quota_exempt_and_fake_worker_models_pins(self):
        for raw in ('{}', '{"opencode": null}', '{"opencode": 0}', 'broken'):
            (self.root / "quota.json").write_text(raw)
            self.assertEqual(core.quota_reasons(self.root, ["opencode"]), [])
            job = dict(id="probe", cwd=str(self.cwd), pin="opencode")
            self.assertEqual(core.check_queue(self.root, job, []), "opencode")
            self.assertEqual(job["queue_reasons"], [])
        (self.root / "quota.json").unlink()
        for tier in ("L", "M", "H"):
            self.assertEqual(core.model_for("opencode", tier),
                             ("opencode-go/deepseek-v4.1-flash", "max"))
        for pool in ("opencode", "codex", "cc"):
            self.submit(pool, "--pin", pool, "--tier", "L", "--prompt-text", "fixture", "--tick")
            self.assertEqual(core.find_job(self.root, pool)["pool"], pool)
            core.reclaim(self.root)
            job = core.find_job(self.root, pool)
            self.assertEqual(job["status"], "done")
            if pool == "opencode":
                self.assertEqual(job["effort"], "max")

    def test_opencode_model_validation_at_submit_and_launch(self):
        for flags in (("--pin", "opencode"), ("--tier", "L")):
            result = self.submit("bad-model", "--prompt-text", "fixture", "--model", "other/model",
                                 *flags, expected=2)
            self.assertIn("opencode/", result["error"])
            self.assertIsNone(core.find_job(self.root, "bad-model"))
            self.assertFalse((self.root / "queue/bad-model.prompt.md").exists())
        job = self.record(model="gpt-6.1-sol")
        with patch.object(core, "remote_dir") as remote:
            with self.assertRaisesRegex(ValueError, "opencode/"):
                core.launch(self.root, job, "opencode")
            remote.assert_not_called()
        core.remove_job(self.root, "queue", job["id"])
        self.submit("custom-open", "--pin", "opencode", "--prompt-text", "fixture",
                    "--model", "opencode/custom", "--tick")
        self.assertEqual(core.find_job(self.root, "custom-open")["model"], "opencode/custom")
        self.assertEqual(core.find_job(self.root, "custom-open")["effort"], "max")

    def test_submit_default_tick_and_opt_out(self):
        args = ("submit", "--project", "fixture", "--area", "research", "--tier", "M",
                "--cwd", str(self.cwd), "--read-only", "--prompt-text", "goal", "--json")
        for jid, flags, status in (("default", (), "running"), ("off", ("--no-tick",), "queued"),
                                   ("explicit", ("--tick",), "running")):
            job = json.loads(self.cli(*args, "--id", jid, *flags))["job"]
            self.assertEqual(job["status"], status)
            if status == "queued":
                self.assertIn({"type": "awaiting_tick"}, job["queue_reasons"])
        with patch.dict(os.environ, {"CS_JOB_ID": "parent"}):
            job = json.loads(self.cli(*args, "--id", "nested-default", "--allow-nested"))["job"]
            self.assertEqual(job["status"], "queued")
            self.assertIn({"type": "awaiting_tick"}, job["queue_reasons"])

    def test_completion_business_lines_and_warning(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.cwd), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init")
        paths = ["app.py", "tests/x.py", "tests_unit/x.py", "__tests__/x.ts", "test/x.py",
                 "test_x.py", "x_test.py", "x.test.ts", "x.spec.ts", "notes.md", "docs/x.py",
                 "dist/x.js", "build/x.js", "lib/dashboard_dist/x.js", "package-lock.json",
                 "pnpm-lock.yaml", "yarn.lock", "other.lock", "binary.dat"]
        for path in paths:
            target = self.cwd / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("base\n")
        git("add", ".")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "base")
        for jid, flags in (("limited", ("--max-added-lines", "5")), ("unlimited", ())):
            self.submit(jid, "--prompt-text", "goal", *flags)
        self.cli("tick")
        for path in paths:
            (self.cwd / path).write_text("base\n" + "added\n" * 5)
        (self.cwd / "binary.dat").write_bytes(b"\0binary\n")
        (self.cwd / "new.py").write_text("new\n" * 3)
        (self.cwd / "new.bin").write_bytes(b"\0binary\n")
        (self.cwd / "invalid.bin").write_bytes(b"\xff\n")
        self.assertEqual(core.reclaim(self.root), 2)
        for jid in ("limited", "unlimited"):
            job = core.find_job(self.root, jid)
            self.assertEqual(job["line_stat"], dict(added=8, deleted=0, files=2))
            self.assertEqual(job["status"], "done")
            warnings = [w for w in job["warnings"] if "business lines" in w]
            self.assertEqual(bool(warnings), jid == "limited")
            summary = json.loads(self.cli("status", jid, "--json"))["job"]
            self.assertEqual(summary["line_stat"], job["line_stat"])
            self.assertEqual(summary["max_added_lines"], 5 if jid == "limited" else None)
        self.assertIn("business lines +8 exceed --max-added-lines 5", core.find_job(self.root, "limited")["warnings"])
        self.assertIn("业务代码 +8/-0（2 文件） 超上限 5", self.cli("status", "limited"))
        (self.cwd / "app.py").write_text("replacement\n" * 5)
        job = core.find_job(self.root, "limited")
        core.completion_scope(job, [])
        self.assertEqual(job["line_stat"], dict(added=8, deleted=1, files=2))
        self.assertEqual(sum("business lines" in w for w in job["warnings"]), 1)
        job["max_added_lines"] = 8
        job["warnings"] = []
        core.completion_scope(job, [])
        self.assertEqual(job["warnings"], [])
        for value in ("-1", "1.5"):
            self.submit("invalid", "--prompt-text", "goal", "--max-added-lines", value, expected=2)

    def test_nested_submit_refused_unless_allowed(self):
        with patch.dict(os.environ, {"CS_JOB_ID": "parent-job"}):
            out = self.cli("submit", "--project", "fixture", "--area", "research", "--tier", "M",
                           "--cwd", str(self.cwd), "--id", "nested", "--read-only", "--json",
                           "--prompt-text", "x", expected=2)
            self.assertIn("nested submit", out)
            self.assertIsNone(core.find_job(self.root, "nested"))
            self.submit("nested-ok", "--allow-nested", "--prompt-text", "x")
        self.assertIsNotNone(core.find_job(self.root, "nested-ok"))

    def test_cc_session_id_and_worker_git_identity(self):
        near = "heggriax\0other@example.invalid\nOther\0heggria@example.invalid\nheggria\0"
        for pool in ("cc", "codex", "opencode"):
            for email, history in ((None, ""), ("configured@example.invalid", ""), ("", ""), ("", near),
                                   ("", near + "\nheggria\0x@example.invalid")):
                with self.subTest(pool=pool, email=email):
                    self.submit("identity", "--prompt-text", "fixture")
                    job = core.find_job(self.root, "identity")
                    def git_output(cwd, *args):
                        if email is None:
                            return None
                        if args == ("config", "user.email"):
                            return email
                        if args == ("rev-parse", "HEAD"):
                            return None
                        self.assertEqual(args, ("log", "-n", "200", "--format=%an%x00%ae"))
                        return history
                    def spawn(*args, **kwargs):
                        if pool == "cc":
                            self.assertEqual(core.load_jobs(self.root, "queue")[0]["cc_session_id"], job["cc_session_id"])
                        return Mock(pid=123)
                    with patch.dict(os.environ, {"CS_TEST_FAKE_WORKER": "0"}), \
                            patch.object(core, "git_output", side_effect=git_output) as git, \
                            patch.object(core.pwd, "getpwuid", return_value=Mock(pw_name="heggria")), \
                            patch.object(core.subprocess, "Popen", side_effect=spawn):
                        core.launch(self.root, job, pool)
                    worker = core.remote_dir(job["id"])
                    script = (worker / "run.sh").read_text()
                    if pool == "cc":
                        self.assertEqual(str(core.uuid.UUID(job["cc_session_id"])), job["cc_session_id"])
                        self.assertIn("--session-id " + job["cc_session_id"], script)
                        self.assertIn("> " + str(worker / "last-message.md"), script)
                        self.assertEqual(core.find_job(self.root, "identity")["cc_session_id"], job["cc_session_id"])
                    for key, value in (("AUTHOR_NAME", "heggria"), ("AUTHOR_EMAIL", "x@example.invalid"),
                                       ("COMMITTER_NAME", "heggria"), ("COMMITTER_EMAIL", "x@example.invalid")):
                        self.assertEqual("GIT_" + key + "=" + value in script, history.endswith("x@example.invalid"))
                    self.assertEqual("export GIT_" in script, history.endswith("x@example.invalid"))
                    self.assertEqual(any(call.args[1] == "log" for call in git.call_args_list), not bool(email))
                    core.remove_job(self.root, "running", "identity")

    def test_opencode_shell_jsonl_flags_prompt_and_exit(self):
        self.submit("open-shell", "--pin", "opencode", "--prompt-text", "goal with 'quotes'\nnext")
        job = core.find_job(self.root, "open-shell")
        model = "opencode/custom; $(touch BAD) `touch BAD2`"
        job.update(model=model, effort="low")
        with patch.dict(os.environ, {"CS_TEST_FAKE_WORKER": "0"}), \
                patch.object(core, "git_output", return_value=None), \
                patch.object(core.subprocess, "Popen", return_value=Mock(pid=123)) as popen:
            core.launch(self.root, job, "opencode")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertIs(popen.call_args.kwargs["stdin"], core.subprocess.DEVNULL)
        self.assertEqual(job["pgid"], 123)
        self.assertEqual(job["effort"], "max")
        worker = core.remote_dir(job["id"])
        script = worker / "run.sh"
        self.assertIn("opencode run -m", script.read_text())
        self.assertIn("#max' --auto --format json", script.read_text())
        self.assertIn("export CS_JOB_ID=open-shell\n", script.read_text())
        fake_bin = self.base / "fake-opencode"
        fake_bin.mkdir()
        executable = fake_bin / "opencode"
        executable.write_text('#!/usr/bin/python3\nimport json, sys\nfrom pathlib import Path\n'
                              'Path("args.json").write_text(json.dumps(sys.argv[1:]))\n'
                              'print(json.dumps({"type":"step_start"}))\n'
                              'print(json.dumps({"type":"tool_use","part":{"text":"ignore"}}))\n'
                              'print(json.dumps({"type":"text","part":{"text":"STATUS: DONE\\n"}}))\n'
                              'print(json.dumps({"type":"text","part":{"text":"完成\\n"}}))\n')
        executable.chmod(0o755)
        env = dict(os.environ, PATH=str(fake_bin) + ":/usr/bin:/bin")
        result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        args = json.loads((self.cwd / "args.json").read_text())
        self.assertEqual(args[:-1], ["run", "-m", model + "#max", "--auto", "--format", "json"])
        self.assertEqual(args[-1], (worker / "prompt.md").read_text().rstrip("\n"))
        self.assertEqual((worker / "conclusion.md").read_text(), "STATUS: DONE\n完成\n")
        self.assertEqual((worker / "exit").read_text(), "0\n")
        self.assertFalse((self.cwd / "BAD").exists())
        self.assertFalse((self.cwd / "BAD2").exists())
        self.assertEqual(list(self.cwd.glob("conclusion*")), [])
        executable.write_text('#!/bin/sh\nexit 7\n')
        result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 7)
        self.assertEqual((worker / "exit").read_text(), "7\n")
        executable.write_text('#!/bin/sh\nprintf "invalid-json\\n"\n')
        result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotEqual((worker / "exit").read_text(), "0\n")

    def test_opencode_doctor_cli_is_required_only_for_selected_pool(self):
        with patch("shutil.which", return_value=None), patch("socket.getaddrinfo", return_value=[]):
            for pool in ("opencode", "codex", None):
                checks = {c["name"]: c for c in core.doctor_checks(str(self.cwd), pool)}
                self.assertEqual(checks["opencode_cli"]["level"], "FAIL" if pool == "opencode" else "WARN")

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
        declaration = ('## 本单声明\n- cwd: %s\n- 可写文件清单 (files): 无\n'
                       '- shared 锁文件 (shared_files): 无\n- area: research\n- read-only: 是'
                       % self.cwd)
        first = self.submit("inline", "--prompt-text", "goal")
        self.assertEqual(first["schema_version"], "1")
        self.assertEqual((self.root / "queue/inline.prompt.md").read_text(),
                       "project rules\n\n" + declaration + "\n\n" + policy_text("companion") + "\n\ngoal")
        self.assertEqual(first["job"]["queue_reasons"][-1]["type"], "awaiting_tick")
        self.submit("stdin", "--prompt", "-", stdin="stdin goal")
        self.assertIn("stdin goal", (self.root / "queue/stdin.prompt.md").read_text())
        prompt = self.base / "prompt.md"
        prompt.write_text("file goal")
        self.submit("file", "--prompt", str(prompt))
        self.assertEqual((self.root / "queue/file.prompt.md").read_text(),
                         "project rules\n\n" + declaration + "\n\n" + policy_text("companion") + "\n\nfile goal")
        self.submit("off", "--prompt-text", "raw", "--no-template")
        self.assertEqual((self.root / "queue/off.prompt.md").read_text(),
                         declaration + "\n\n" + policy_text("companion") + "\n\nraw")

    def test_context_prompt_template_and_retries(self):
        rule = "先读 README.md、DESIGN.md、BACKLOG.md 和目标代码，再做最小改动。"
        (self.root / "projects/fixture.md").write_text("- " + rule + "\n")
        source, absolute = self.cwd / "notes.md", self.base / "absolute.md"
        source.write_text("one\n二\nthree\n")
        absolute.write_text("absolute\nlast")
        with patch.dict(os.environ, {"HOME": str(self.base)}):
            self.assertEqual(CLI["context_text"](self.cwd, ["~/absolute.md"]),
                             "### ~/absolute.md:1-2\nabsolute\nlast")
        self.submit("context", "--prompt-text", rule, "--context", "notes.md:2-3",
                    "--context", str(absolute))
        job = core.find_job(self.root, "context")
        expected = "### notes.md:2-3\n二\nthree\n\n\n### %s:1-2\nabsolute\nlast" % absolute
        self.assertEqual(job["context_text"], expected)
        prompt = Path(job["prompt_file"]).read_text()
        self.assertTrue(prompt.endswith("\n\n## 预读上下文\n\n" + expected))
        self.assertIn("- 预读上下文已附，仅在不够时再读", prompt)
        self.assertIn("\n\n" + rule + "\n\n", prompt)  # Only the template changes.
        self.submit("plain", "--prompt-text", "goal")
        self.assertTrue((self.root / "queue/plain.prompt.md").read_text().startswith("- " + rule))
        self.assertNotIn("context_text", core.find_job(self.root, "plain"))
        self.cli("cancel", "context", "--json")
        source.unlink()
        absolute.unlink()
        retried = json.loads(self.cli("retry", "context", "--json"))
        automatic = core.retry_transient(self.root, job)
        for jid in (retried["id"], automatic):
            clone = core.find_job(self.root, jid)
            self.assertEqual(clone["context_text"], expected)
            self.assertTrue(Path(clone["prompt_file"]).read_text().endswith(expected))

    def test_context_rejects_invalid_sensitive_paths_and_size_without_queueing(self):
        source = self.cwd / "notes.md"
        source.write_text("ok\n")
        sensitive = [".env", ".env.local", ".ssh/x", ".gnupg/x", ".aws/x", "auth.json",
                     ".npmrc", ".netrc", ".pypirc", "credentials", "id_rsa.pub", "id_ed25519.x",
                     ".ENV", ".SSH/x", "AUTH.JSON", ".git-credentials", ".CONFIG/GH/hosts.yml",
                     ".KUBE/config", ".DOCKER/config.json"]
        for name in sensitive:
            path = self.cwd / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("secret")
        (self.cwd / "alias").symlink_to(self.cwd / ".env")
        (self.cwd / "ssh-alias").symlink_to(self.cwd / ".ssh")
        sensitive.append("ssh-alias/x")
        with patch.object(Path, "open", side_effect=AssertionError("must reject before open")):
            for name in sensitive:
                with self.assertRaisesRegex(ValueError, "credential"):
                    CLI["context_text"](self.cwd, [name])
        for index, spec in enumerate(sensitive + ["alias", "missing", ".", "", "notes.md:0-1",
                                                  "notes.md:2-1", "notes.md:-1-2", "notes.md:1-x"]):
            with self.subTest(spec=spec):
                self.assertIn("error", self.submit("bad%d" % index, "--prompt-text", "goal",
                                                  "--context", spec, expected=2))
        source.write_text("中" * 22000)
        result = self.submit("large", "--prompt-text", "goal", "--context", "notes.md", expected=2)
        self.assertIn("line ranges", result["error"])
        self.assertEqual(list((self.root / "queue").iterdir()), [])
        source.write_text("x" * (CLI["CONTEXT_LIMIT"] - len("### notes.md:1-1\n")))
        self.assertEqual(len(CLI["context_text"](self.cwd, ["notes.md"]).encode()), 65536)
        with self.assertRaisesRegex(ValueError, "line ranges"):
            CLI["context_text"](self.cwd, ["notes.md", "notes.md"])
        with patch.object(Path, "home", return_value=self.base):
            for name in (".codex/nested/data.json", ".claude/data.json", ".CODEX/x.JSON"):
                with self.assertRaisesRegex(ValueError, "credential"):
                    CLI["context_text"](self.cwd, [str(self.base / name)])

    def test_context_bounded_reads_and_large_file_ranges(self):
        source = self.cwd / "large.txt"
        source.write_text("x" * 200000 + "\nsmall\n" + "z" * 200000)
        result = self.submit("too-large", "--prompt-text", "goal", "--context", "large.txt", expected=2)
        self.assertIn("64KB", result["error"])
        self.assertEqual(list((self.root / "queue").iterdir()), [])  # No JSON or prompt.
        stream = io.StringIO(source.read_text())
        bounded_readline = stream.readline
        def readline(size=-1):
            self.assertTrue(0 < size <= CLI["CONTEXT_LIMIT"] + 1)
            return bounded_readline(size)
        with patch.object(Path, "open", return_value=stream), \
                patch.object(stream, "close"), \
                patch.object(stream, "read", side_effect=AssertionError("unbounded read")), \
                patch.object(stream, "readline", side_effect=readline):
            self.assertEqual(CLI["context_text"](self.cwd, ["large.txt:2-2"]), "### large.txt:2-2\nsmall\n")
            self.assertEqual(stream.tell(), 200007)  # Stop at the requested end line.
        self.submit("small-range", "--prompt-text", "goal", "--context", "large.txt:2-2")
        self.assertEqual(core.find_job(self.root, "small-range")["context_text"], "### large.txt:2-2\nsmall\n")
        source.write_bytes(b"x" * 65536 + b"\r\nsmall\rfinal")
        self.assertEqual(CLI["context_text"](self.cwd, ["large.txt:2-3"]), "### large.txt:2-3\nsmall\rfinal")

    def test_submit_declaration_for_write_read_only_and_no_template(self):
        (self.root / "projects/fixture.md").write_text("project rules\n")
        cases = [
            ("write", ["--files", " src/a.py, src/b.py ", "--lock-shared", " src/core.py "],
             '["src/a.py", "src/b.py"]', '["src/core.py"]', "否"),
            ("readonly", ["--read-only", "--files", "review.md"], '["review.md"]', "无", "是"),
            ("no-template", ["--no-template", "--files", "task.py"], '["task.py"]', "无", "否"),
        ]
        for jid, flags, files, shared, read_only in cases:
            with self.subTest(jid=jid):
                self.submit(jid, "--area", "backend", "--prompt-text", "task goal", *flags)
                prompt = (self.root / ("queue/%s.prompt.md" % jid)).read_text()
                declaration = ('## 本单声明\n- cwd: %s\n- 可写文件清单 (files): %s\n'
                               '- shared 锁文件 (shared_files): %s\n- area: backend\n- read-only: %s'
                               % (self.cwd, files, shared, read_only))
                prefix = "" if jid == "no-template" else "project rules\n\n"
                self.assertEqual(prompt, prefix + declaration + "\n\n" + policy_text("companion") + "\n\ntask goal")

    def test_scope_policy_prompt_and_warning_output(self):
        for policy in ("companion", "strict", "flex"):
            flags = [] if policy == "companion" else ["--scope-policy", policy]
            result = self.submit(policy, "--area", "backend", "--files", "src/x.py",
                                 "--prompt-text", "goal", *flags)
            job = core.find_job(self.root, policy)
            self.assertEqual(job["scope_policy"], policy)
            self.assertEqual(bool(result["warnings"]), policy == "strict")
            prompt = Path(job["prompt_file"]).read_text()
            self.assertIn("改动范围策略 (scope policy): " + policy, prompt)
            self.assertIn("OUT_OF_SCOPE_FILES:", prompt)
            self.assertIn("NEED_FILES: path1, path2", prompt)
            if policy == "strict":
                self.assertIn("stay strictly within --files", prompt)
            else:
                self.assertIn("overrides generic strict wording", prompt)
            shared = self.submit(policy + "-shared", "--area", "backend", "--files", "review.md",
                                 "--lock-shared", "a.py,b.py,c.py,d.py", "--prompt-text", "goal", *flags)
            self.assertEqual(len(shared["warnings"]), 1 + 4 * ("companion", "strict", "flex").index(policy))
            self.assertIn("serialize sibling jobs on the same repo", shared["warnings"][0])
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()), \
                patch.object(sys, "argv", ["cs", "submit", "--project", "fixture", "--area", "backend",
                                          "--tier", "M", "--cwd", str(self.cwd), "--files", "package.json",
                                          "--scope-policy", "strict", "--prompt-text", "goal"]):
            self.assertEqual(CLI["main"](), 0)
        self.assertIn("consider adding its lock file", stderr.getvalue())

    def test_need_files_and_retry_merge_preserves_original(self):
        self.submit("original", "--area", "backend", "--files", "src/x.py", "--lock-shared", "core.py",
                    "--prompt-text", "raw goal", "--model", "custom-model", "--no-template", "--tick")
        worker = core.remote_dir("original")
        (worker / "conclusion.md").write_text("STATUS: BLOCKED\n NEED_FILES: tests/test_x.py, extra.py \n"
                                             "NEED_FILES: extra.py, more.py\n")
        original = json.loads(self.cli("wait", "original"))["jobs"][0]
        self.assertEqual(original["need_files"], ["tests/test_x.py", "extra.py", "more.py"])
        path = self.root / "done/original.json"
        before = path.read_bytes()
        result = json.loads(self.cli("retry", "original", "--add-files", "extra.py,manual.py",
                                     "--add-shared", "extra-core.py", "--use-need-files",
                                     "--scope-policy", "flex", "--idempotency-key", "retry-1", "--json"))
        job = core.find_job(self.root, result["id"])
        self.assertEqual(job["files"], ["src/x.py", "extra.py", "manual.py", "tests/test_x.py", "more.py"])
        self.assertEqual(job["shared_files"], ["core.py", "extra-core.py"])
        self.assertEqual(job["retry_of"], "original")
        self.assertEqual(job["scope_policy"], "flex")
        self.assertEqual(job["user_prompt"], "raw goal")
        self.assertEqual(job["model"], "custom-model")
        prompt = Path(job["prompt_file"]).read_text()
        self.assertEqual(prompt.count("## 本单声明"), 1)
        self.assertTrue(prompt.endswith("raw goal"))
        self.assertEqual(path.read_bytes(), before)
        again = json.loads(self.cli("retry", "original", "--idempotency-key", "retry-1", "--json"))
        self.assertTrue(again["existing"])
        self.assertEqual(again["id"], job["id"])
        self.cli("tick", "--json")
        self.cli("wait", job["id"])
        result = json.loads(self.cli("retry", job["id"], "--tick", "--json"))
        self.assertEqual(result["job"]["status"], "running")

    def test_retry_refuses_active_missing_and_legacy_jobs(self):
        self.submit("queued", "--prompt-text", "goal")
        self.cli("retry", "queued", "--json", expected=2)
        self.submit("running", "--prompt-text", "goal", "--tick")
        self.cli("retry", "running", "--json", expected=2)
        self.cli("retry", "missing", "--json", expected=1)
        self.record("legacy", "done", status="blocked")
        self.cli("retry", "legacy", "--json", expected=2)

    def test_prompt_sources_mutually_exclusive_and_required_json_errors(self):
        for extra in [(), ("--prompt-text", "x", "--prompt", "-")]:
            result = self.submit("bad", *extra, expected=2)
            self.assertEqual(result["schema_version"], "1")
            self.assertIn("error", result)
        result = self.submit("missing", "--prompt", str(self.base / "missing"), expected=2)
        self.assertIn("error", result)
        self.assertFalse((self.root / "queue/missing.json").exists())

    def test_submit_empty_files_rejected_before_reading_or_writing_prompt(self):
        for index, flags in enumerate([(), ("--files", ""), ("--files", " , , ")]):
            jid = "empty-%s" % index
            result = self.submit(jid, "--area", "backend", "--prompt", "-",
                                 *flags, expected=2)
            self.assertEqual(result["schema_version"], "1")
            self.assertIn("--files is required", result["error"])
            self.assertEqual(list((self.root / "queue").iterdir()), [])
        with patch.object(sys, "stdin", Mock(read=Mock(side_effect=AssertionError("read prompt")))), \
                patch.object(sys, "argv", ["cs", "submit", "--project", "fixture", "--area", "backend",
                                          "--tier", "M", "--cwd", str(self.cwd), "--prompt", "-"]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(CLI["main"](), 2)
        self.assertIn("--files is required", err.getvalue())

    def test_submit_empty_files_exemptions_and_exact_files(self):
        areas = ("backend", "protocol", "frontend", "ui-copy", "test", "review", "ops-script", "research")
        for area in areas:
            for read_only in (False, True):
                for files in ("", " , , ", " a.py, b.py "):
                    with self.subTest(area=area, read_only=read_only, files=files):
                        jid = "case-%s" % len(list((self.root / "queue").glob("*.json")))
                        flags = ["--read-only"] if read_only else []
                        accepted = bool(files.strip(" ,")) or (area == "research" and read_only)
                        result = self.submit(jid, "--area", area, "--prompt-text", "goal",
                                             "--files", files, *flags, expected=0 if accepted else 2)
                        self.assertEqual(result["schema_version"], "1")
                        if accepted:
                            job = core.find_job(self.root, jid)
                            self.assertEqual(job["files"], ["a.py", "b.py"] if files.strip(" ,") else [])
                            self.assertEqual(job["read_only"], read_only)
                            self.assertTrue(Path(job["prompt_file"]).is_file())
                        else:
                            for text in ("--files is required", "review docs", "never a directory",
                                         "--area research --read-only", "逐一", "评审文档"):
                                self.assertIn(text, result["error"])
                            self.assertIsNone(core.find_job(self.root, jid))

    def test_submit_reports_blockers_immediately_with_or_without_tick(self):
        (self.root / "quota.json").write_text('{"codex": 0, "cc": 0}')
        for jid, flags in [("queued", []), ("tick", ["--tick"])]:
            result = self.submit(jid, "--prompt-text", "x", "--pin", "codex", *flags)
            self.assertIn("quota_exhausted", [r["type"] for r in result["job"]["queue_reasons"]])
        output = self.cli("submit", "--project", "p", "--area", "backend", "--tier", "L",
                          "--cwd", str(self.cwd), "--prompt-text", "x", "--files", "task.py",
                          "--pin", "codex", "--tick")
        self.assertIn("额度耗尽", output)

    def test_submit_tick_json_contains_only_submitted_job(self):
        self.submit("other", "--prompt-text", "another task")
        result = self.submit("mine", "--prompt-text", "my task", "--tick")
        self.assertEqual(set(result), {"schema_version", "id", "existing", "job", "warnings"})
        self.assertNotIn("other", json.dumps(result))
        self.assertEqual(result["job"]["status"], "running")
        self.assertEqual(core.find_job(self.root, "other")["status"], "running")
        (self.root / "quota.json").write_text('{"codex": 0, "cc": 0}')
        queued = self.submit("blocked-mine", "--prompt-text", "queued task", "--tick")
        self.assertNotIn("other", json.dumps(queued))
        self.assertIn("quota_exhausted", [r["type"] for r in queued["job"]["queue_reasons"]])

    def test_lock_queue_and_board_reasons_are_unique(self):
        self.record("active", "running", status="running", pid=os.getpid(), pool="codex",
                    files=["src/", "src/a.py", "src/a.py"])
        queued = self.record(files=["src/a.py", "./src/a.py", "src/a.py"])
        core.tick(self.root)
        queued = core.find_job(self.root, queued["id"])
        locks = [r for r in queued["queue_reasons"] if r["type"] == "lock"]
        self.assertEqual(len(locks), 1)
        self.assertEqual(len(queued["blocked_by"]), 1)
        # Also deduplicate persisted legacy reason lists when rendering the board.
        queued["queue_reasons"] *= 3
        core.write_job(self.root, "queue", queued)
        board = core.write_board(self.root).read_text()
        self.assertEqual(board.count("active 占用 src/a.py (write锁)"), 1)

    def test_tick_log_summary_rotation_and_configurable_retention(self):
        self.record("queued", pin="codex")
        (self.root / "quota.json").write_text('{"codex": 0}')
        result = json.loads(self.cli("tick"))
        self.assertEqual(result["queued_count"], 1)
        self.assertNotIn("queue_reasons", result)
        path = self.root / "logs/tick.log"
        core.tick(self.root)
        self.assertEqual(len(path.read_text().splitlines()), 2)
        for line in path.read_text().splitlines():
            summary = json.loads(line)
            self.assertEqual(summary["queued_count"], 1)
            self.assertNotIn("queued", summary)
        with patch.dict(os.environ, {"CS_TICK_LOG_MAX_BYTES": "512", "CS_TICK_LOG_BACKUPS": "2"}):
            for _ in range(10):
                core.tick(self.root)
            logs = list(path.parent.glob("tick.log*"))
            self.assertEqual(len(logs), 3)
            self.assertTrue(all(log.stat().st_size <= 512 for log in logs))
        with patch.dict(os.environ, {"CS_TICK_LOG_MAX_BYTES": "256", "CS_TICK_LOG_BACKUPS": "0"}):
            for _ in range(3):
                core.tick(self.root)
            self.assertEqual(list(path.parent.glob("tick.log*")), [path])
            self.assertLessEqual(path.stat().st_size, 256)

    def test_tick_log_redirected_stdout_stays_bounded_without_duplicate_lines(self):
        path = self.root / "logs/tick.log"
        path.parent.mkdir()
        env = dict(os.environ, CS_TICK_LOG_MAX_BYTES="512", CS_TICK_LOG_BACKUPS="2")
        # Same append descriptor across invocations, as with launchd stdout.
        with path.open("a") as output:
            for _ in range(10):
                result = subprocess.run(["/usr/bin/python3", str(REPO / "bin/cs"), "tick"],
                                        env=env, stdout=output, stderr=subprocess.PIPE, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
        logs = list(path.parent.glob("tick.log*"))
        self.assertEqual(len(logs), 3)
        self.assertTrue(all(log.stat().st_size <= 512 for log in logs))
        lines = [line for log in logs for line in log.read_text().splitlines()]
        self.assertTrue(lines)
        for line in lines:
            self.assertEqual(json.loads(line)["queued_count"], 0)
            self.assertNotIn("at", json.loads(line))  # stdout is the only log writer.

    def test_directory_warning_once_per_normalized_path_and_never_on_tick(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            args = ["cs", "submit", "--project", "p", "--area", "research", "--tier", "M",
                    "--cwd", str(self.cwd), "--id", "dir", "--prompt-text", "goal", "--json",
                    "--files", ".,./," + str(self.cwd), "--lock-shared", str(self.cwd) + "/"]
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(CLI["main"](), 0)
            core.tick(self.root)
            core.tick(self.root)
            with patch.object(sys, "argv", args), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(CLI["main"](), 2)  # duplicate submission cannot warn again.
        self.assertEqual(stderr.getvalue().count("WARNING: directory lock"), 1)

    def test_idempotency_all_folders_and_no_prompt_overwrite(self):
        first = self.submit("original", "--prompt-text", "original", "--idempotency-key", "key")
        original_prompt = Path(core.find_job(self.root, "original")["prompt_file"]).read_text()
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
            saved = core.find_job(self.root, "original")
            self.assertEqual(Path(saved["prompt_file"]).read_text(), original_prompt)
        self.assertFalse((self.root / "queue/original.prompt.md").exists())

    def test_duplicate_id_rejected_and_generated_ids_do_not_collide(self):
        self.submit("same", "--prompt-text", "first")
        original_prompt = (self.root / "queue/same.prompt.md").read_text()
        self.assertIn("error", self.submit("same", "--prompt-text", "second", expected=2))
        self.assertEqual((self.root / "queue/same.prompt.md").read_text(), original_prompt)
        args = ("submit", "--project", "p", "--area", "test", "--tier", "L", "--cwd", str(self.cwd),
                "--prompt-text", "goal", "--files", "task.py", "--json")
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
                                       ("blocked", 0, "STATUS: BLOCKED", "blocked"),
                                       ("empty", 0, "", "unknown")]:
            self.record(jid, "running", pid=None, status="running")
            jd = core.remote_dir(jid)
            jd.mkdir(parents=True)
            (jd / "exit").write_text(str(code))
            (jd / "conclusion.txt").write_text(text)
            core.reclaim(self.root)
            self.assertEqual(core.find_job(self.root, jid)["status"], status)
            if jid == "empty":
                self.assertEqual(core.find_job(self.root, jid)["outcome_reason"], "missing_conclusion")

    def test_classify_outcome_requires_exact_structured_marker(self):
        conclusion = self.base / "result.txt"
        for body, expected in [
                ("STATUS: BLOCKED", "blocked"),
                ("Summary\n  STATUS: BLOCKED  \nReason", "blocked"),
                ("STATUS: DONE\n未合并/只读/未提交", "done"),
                ("Mention STATUS: BLOCKED in prose", "done"),
                ("STATUS: BLOCKED because waiting", "done"),
                ("status: blocked", "done"),
                ("FAIL is an ordinary word here", "done"),
                ("   \n", "unknown")]:
            conclusion.write_text(body)
            for job in (None, {"area": "frontend"}, {"area": "research"}, {"read_only": True}):
                with self.subTest(body=body, job=job):
                    self.assertEqual(core.classify_outcome(0, conclusion, job)[0], expected)
            for code, status in [(7, "failed"), (124, "unknown"), (None, "unknown")]:
                self.assertEqual(core.classify_outcome(code, conclusion)[0], status)
        conclusion.unlink()
        self.assertEqual(core.classify_outcome(0, conclusion), ("unknown", None))
        self.assertEqual(core.classify_outcome(0, None), ("unknown", None))

    def test_delivered_blocked_keeps_raw_status_and_warning(self):
        sha = "f7fd" + "a" * 36
        bodies = ["Implementation complete: Draft PR #249 SHA: " + sha,
                  "Created PR #249", "PR #249 已创建", "Pushed commit " + sha,
                  "已推送 commit " + sha, "HEAD: " + sha, "Pushed commit:\n" + sha,
                  "Delivered https://github.com/example/repo/pull/249",
                  "Delivered https://example.invalid/repo/pulls/249"]
        for index, body in enumerate(bodies):
            jid = "delivered-%s" % index
            self.submit(jid, "--prompt-text", "deliver", "--tick")
            worker = core.remote_dir(jid)
            (worker / "conclusion.md").write_text(body + "\nSTATUS: BLOCKED\nCI pending")
            core.reclaim(self.root)
            job = core.find_job(self.root, jid)
            with self.subTest(body=body):
                self.assertEqual(job["status"], "done")
                self.assertEqual(job["raw_status"], "blocked")
                self.assertIn("delivered_but_blocked: CI pending", job["warnings"])
                summary = core.job_summary(job)
                self.assertEqual(summary["raw_status"], "blocked")
                self.assertEqual(summary["warnings"], job["warnings"])

    def test_blocked_without_delivery_and_exit_precedence(self):
        sha = "2dc5" + "b" * 36
        result = self.base / "result.txt"
        for body in ("未改代码、未开 PR。HEAD：" + sha,
                     "no code changes\nHEAD: " + sha,
                     "未提交\ncommit " + sha,
                     "未开 PR https://github.com/example/repo/pull/249",
                     "not pushed commit " + sha, "Will create PR #249",
                     "PR #249 exists", "No PR #249 created", "pushed commit f7fd1234"):
            result.write_text("STATUS: BLOCKED\n" + body)
            job = {}
            with self.subTest(body=body):
                self.assertEqual(core.classify_outcome(0, result, job)[0], "blocked")
                self.assertNotIn("raw_status", job)
        result.write_text("STATUS: BLOCKED\nDraft PR #249")
        for code, expected in ((1, "failed"), (124, "unknown"), (None, "unknown")):
            self.assertEqual(core.classify_outcome(code, result)[0], expected)

    def test_opencode_idle_reclaim_kills_group_and_preserves_timeout_reason(self):
        for index, text in enumerate((None, "Insufficient account funds")):
            jid = "idle-%s" % index
            self.record(jid, "running", pool="opencode", pid=os.getpid(), pgid=12345,
                        started_at=core.datetime.fromtimestamp(100, core.TZ).isoformat())
            worker = core.remote_dir(jid)
            worker.mkdir(parents=True)
            if text:
                (worker / "output.jsonl").write_text(json.dumps({"type": "error", "error": text}))
                os.utime(worker / "output.jsonl", (100, 100))
            with patch.object(core.time, "time", return_value=700), \
                    patch.object(core, "kill_worker_group", return_value={"cancel_signal": "SIGTERM"}) as kill:
                self.assertEqual(core.reclaim(self.root), 1)
                kill.assert_called_once_with(12345)
            job = core.find_job(self.root, jid)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["exit"], 1)
            self.assertEqual(job["failure_reason"], "no_output_timeout")
            self.assertTrue(job["failure_detail"])
            self.assertNotIn("retried_as", job)

    def test_opencode_idle_tracks_both_files_mtime_size_and_config(self):
        worker = core.remote_dir("activity")
        worker.mkdir(parents=True)
        for filename in ("log", "output.jsonl"):
            job = dict(id="activity", started_at=core.datetime.fromtimestamp(100, core.TZ).isoformat())
            self.assertFalse(core.opencode_idle(job, 699))
            path = worker / filename
            path.write_text("text")
            os.utime(path, (700, 700))
            self.assertFalse(core.opencode_idle(job, 700))
            self.assertFalse(core.opencode_idle(job, 1299))
            self.assertTrue(core.opencode_idle(job, 1300))
            # Same size with new mtime, then size change with unchanged mtime.
            os.utime(path, (1300, 1300))
            self.assertFalse(core.opencode_idle(job, 1300))
            path.write_text("longer text")
            os.utime(path, (1300, 1300))
            self.assertFalse(core.opencode_idle(job, 1400))
            path.unlink()
            self.assertFalse(core.opencode_idle(job, 1500))
        for value in ("bad", "0", "-1", "nan", "inf"):
            with patch.dict(os.environ, {"CS_OPENCODE_IDLE_S": value}):
                self.assertFalse(core.opencode_idle(job, 2099))
                self.assertTrue(core.opencode_idle(job, 2100))
        with patch.dict(os.environ, {"CS_OPENCODE_IDLE_S": "10"}):
            self.assertFalse(core.opencode_idle(job, 1509))
            self.assertTrue(core.opencode_idle(job, 1510))
        # A delayed tick must use the write time, not grant another full idle window.
        path.write_text("delayed write")
        os.utime(path, (1600, 1600))
        self.assertTrue(core.opencode_idle(job, 2200))

    def test_idle_guard_only_running_opencode_without_exit(self):
        self.record("codex-live", "running", pool="codex", pid=os.getpid())
        self.record("open-exit", "running", pool="opencode", pid=os.getpid())
        worker = core.remote_dir("open-exit")
        worker.mkdir(parents=True)
        (worker / "exit").write_text("124")
        with patch.object(core, "opencode_idle") as idle, patch.object(core, "kill_worker_group") as kill:
            self.assertEqual(core.reclaim(self.root), 1)
            idle.assert_not_called()
            kill.assert_not_called()
        self.assertEqual(core.find_job(self.root, "open-exit")["status"], "unknown")

    def test_opencode_known_plain_and_json_errors(self):
        cases = [("HTTP 429", "rate_limit"), ("rate limit exceeded", "rate_limit"),
                 ("Insufficient account funds", "insufficient_funds"),
                 ("Model unavailable", "model_unavailable"), ("provider.no-route", "model_unavailable"),
                 ("authentication failed", "auth"), ("Unauthorized 401", "auth"),
                 ("invalid API key", "auth"), ("auth error", "auth"), ("ProviderAuthError", "auth")]
        for index, (text, expected) in enumerate(cases):
            for filename in ("log", "output.jsonl"):
                jid = "error-%s-%s" % (index, filename)
                self.record(jid, "running", pool="opencode")
                worker = core.remote_dir(jid)
                worker.mkdir(parents=True)
                (worker / "exit").write_text("124" if index == 0 else "7")
                (worker / filename).write_text(text if filename == "log" else
                                              json.dumps({"type": "error", "error": {"message": text}}))
                core.reclaim(self.root)
                job = core.find_job(self.root, jid)
                with self.subTest(text=text, filename=filename):
                    self.assertEqual(job["failure_reason"], expected)
                    self.assertIn(text, job["failure_detail"])
                    self.assertEqual(job["status"], "unknown" if index == 0 else "failed")
                    self.assertNotIn("retried_as", job)
        self.assertEqual(core.opencode_failure('{"type": "error", "message": "unexpected"}')[0],
                         "opencode_error")
        self.assertEqual(core.opencode_failure("unrecognized failure"), (None, None))
        self.assertEqual(core.opencode_failure("authentication succeeded; unit tests failed"), (None, None))

    def test_opencode_persistent_error_takes_precedence_over_old_transient(self):
        self.submit("funds", "--pin", "opencode", "--prompt-text", "goal", "--tick")
        worker = core.remote_dir("funds")
        (worker / "exit").write_text("1")
        (worker / "log").write_text("Service Unavailable\nInsufficient account funds")
        core.reclaim(self.root)
        job = core.find_job(self.root, "funds")
        self.assertEqual(job["failure_reason"], "insufficient_funds")
        self.assertNotIn("retried_as", job)
        self.assertEqual(core.load_jobs(self.root, "queue"), [])

    def test_upstream_transient_requeues_once_and_preserves_submission(self):
        template = self.root / "projects/fixture.md"
        template.write_text("original template")
        self.submit("transient", "--area", "backend", "--files", "src/x.py", "--lock-shared", "core.py",
                    "--scope-policy", "flex", "--pin", "codex", "--model", "custom-model",
                    "--prompt-text", "raw goal", "--idempotency-key", "original-key", "--tick")
        worker = core.remote_dir("transient")
        (worker / "exit").write_text("1")
        (worker / "log").write_text("Transport channel closed")
        template.write_text("current template")
        self.assertEqual(core.reclaim(self.root), 1)
        original = core.find_job(self.root, "transient")
        retry = core.find_job(self.root, original["retried_as"])
        self.assertEqual(original["failure_reason"], "upstream_transient")
        self.assertEqual(original["status"], "failed")
        self.assertEqual(retry["status"], "queued")
        self.assertEqual(retry["retry_of"], "transient")
        for key in ("project", "area", "tier", "cwd", "files", "shared_files", "scope_policy",
                    "pin", "requested_model", "read_only", "user_prompt", "no_template"):
            self.assertEqual(retry[key], original[key], key)
        self.assertIsNone(retry["idempotency_key"])
        prompt = Path(retry["prompt_file"]).read_text()
        self.assertTrue(prompt.startswith("current template"))
        self.assertTrue(prompt.endswith("raw goal"))
        self.assertEqual(prompt.count("## 本单声明"), 1)
        core.reclaim(self.root)
        self.assertEqual(len(core.load_jobs(self.root, "queue")), 1)
        core.tick(self.root)
        worker = core.remote_dir(retry["id"])
        (worker / "exit").write_text("7")
        (worker / "conclusion.md").write_text("API Error: 503 Service Unavailable")
        core.reclaim(self.root)
        retry = core.find_job(self.root, retry["id"])
        self.assertEqual(retry["failure_reason"], "upstream_transient")
        self.assertEqual(retry["status"], "failed")
        self.assertNotIn("retried_as", retry)
        self.assertEqual(core.load_jobs(self.root, "queue"), [])

    def test_transient_matching_exit_gate_and_legacy_prompt(self):
        errors = ["Transport channel closed", "error sending request for url (https://chatgpt.com/api)",
                  "model is at capacity", "HTTP 502", "HTTP 503", "HTTP 504", "API Error: 500",
                  "API Error: 502", "API Error: 503", "API Error: 504", "Bad Gateway",
                  "Service Unavailable", "connection reset by peer"]
        for index, text in enumerate(errors):
            jid = "legacy-%s" % index
            prompt = self.root / "queue" / (jid + ".prompt.md")
            prompt.write_text("legacy exact prompt")
            self.record(jid, "running", project="fixture", area="backend", tier="M",
                        prompt_file=str(prompt), pool="codex")
            worker = core.remote_dir(jid)
            worker.mkdir(parents=True)
            (worker / "exit").write_text("1")
            (worker / "log").write_text(text)
            core.reclaim(self.root)
            original = core.find_job(self.root, jid)
            self.assertEqual(original["failure_reason"], "upstream_transient", text)
            retry = core.find_job(self.root, original["retried_as"])
            self.assertEqual(Path(retry["prompt_file"]).read_text(), "legacy exact prompt")
        count = len(core.load_jobs(self.root, "queue"))
        for index, (code, text, status) in enumerate([
                (0, "STATUS: BLOCKED\nBad Gateway", "blocked"),
                (0, "STATUS: DONE\nBad Gateway", "done"),
                (124, "Bad Gateway", "unknown"), (None, "Bad Gateway", "unknown"),
                (2, "unit test assertion failed", "failed")]):
            jid = "no-retry-%s" % index
            self.record(jid, "running", pool="codex")
            worker = core.remote_dir(jid)
            worker.mkdir(parents=True)
            if code is not None:
                (worker / "exit").write_text(str(code))
            (worker / "conclusion.md").write_text(text)
            core.reclaim(self.root)
            self.assertEqual(core.find_job(self.root, jid)["status"], status)
            self.assertNotIn("retried_as", core.find_job(self.root, jid))
        self.assertEqual(len(core.load_jobs(self.root, "queue")), count)
        self.record("cancelled-transient", "done", status="cancelled", exit=1)
        self.assertEqual(core.reclaim(self.root), 0)
        self.assertEqual(len(core.load_jobs(self.root, "queue")), count)

    def test_worker_prompt_base_failure_guidance_for_every_pool(self):
        for pool in ("codex", "cc", "opencode"):
            self.submit("guide-" + pool, "--prompt-text", "goal", "--pin", pool, "--no-template", "--tick")
            prompt = (core.remote_dir("guide-" + pool) / "prompt.md").read_text()
            self.assertIn("Verify by running the same checks on base", prompt)
            self.assertIn("'Pre-existing failures'", prompt)
            self.assertIn("and still deliver", prompt)

    def test_legacy_transient_missing_prompt_still_finishes_with_explanation(self):
        self.record("missing-prompt", "running", pool="codex", prompt_file=None)
        worker = core.remote_dir("missing-prompt")
        worker.mkdir(parents=True)
        (worker / "exit").write_text("1")
        (worker / "log").write_text("Transport channel closed")
        self.assertEqual(core.reclaim(self.root), 1)
        job = core.find_job(self.root, "missing-prompt")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure_reason"], "upstream_transient")
        self.assertIn("original prompt unavailable", job["retry_error"])
        self.assertNotIn("retried_as", job)

    def test_launch_preserves_last_queue_reasons_and_wait_in_done_and_event(self):
        self.record("blocker", "running", pool="codex", pid=os.getpid(), shared_files=["src/x.py"])
        self.submit("waiting", "--files", "src/x.py", "--prompt-text", "goal")
        core.tick(self.root)
        queued = core.find_job(self.root, "waiting")
        reasons = queued["queue_reasons"]
        self.assertEqual(reasons[0]["type"], "lock")
        core.remove_job(self.root, "running", "blocker")
        core.tick(self.root)
        core.reclaim(self.root)
        done = core.find_job(self.root, "waiting")
        self.assertEqual(done["last_queue_reasons"], reasons)
        self.assertEqual(done["queue_reasons"], [])
        self.assertGreaterEqual(done["queue_wait_s"], 0)
        event = json.loads((self.root / "logs/events.jsonl").read_text())
        self.assertEqual(event["queue_reason_types"], ["lock"])
        self.assertEqual(event["last_queue_reasons"], reasons)
        self.assertEqual(event["queue_wait_s"], done["queue_wait_s"])

    def test_submit_warns_for_queued_and_running_shared_overlaps(self):
        self.record("active", "running", shared_files=["src"], pid=os.getpid())
        self.record("pending", "queue", shared_files=["src/core.py"])
        self.record("ordinary", "queue", files=["src/core.py"])
        args = ["cs", "submit", "--project", "fixture", "--area", "backend", "--tier", "M",
                "--cwd", str(self.cwd), "--id", "overlap", "--files", "src/core.py",
                "--lock-shared", "src/other.py", "--prompt-text", "goal", "--json"]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", args), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(CLI["main"](), 0)
        warnings = json.loads(out.getvalue())["warnings"]
        for jid in ("active", "pending"):
            self.assertIn("shared lock overlap with " + jid, err.getvalue())
        self.assertIn("src/core.py", err.getvalue())
        self.assertIn("src/other.py", err.getvalue())
        self.assertIn("serialize", err.getvalue())
        self.assertNotIn("ordinary", err.getvalue())
        self.assertEqual(len(warnings), 3)
        self.assertTrue(all(warning in err.getvalue() for warning in warnings))
        self.assertEqual(core.find_job(self.root, "overlap")["status"], "queued")

    def test_dashboard_v2_conclusion_is_done(self):
        body = (REPO / "tests_cs/fixtures/cs-dashboard-v2.txt").read_text()
        self.record("dashboard-v2", "running", status="running", area="frontend")
        worker = core.remote_dir("dashboard-v2")
        worker.mkdir(parents=True)
        (worker / "exit").write_text("0")
        (worker / "conclusion.md").write_text(body)
        core.reclaim(self.root)
        job = core.find_job(self.root, "dashboard-v2")
        self.assertEqual(job["status"], "done")
        self.assertFalse(job.get("block_reasons"))

    def test_terminal_outcomes_archive_queue_prompt(self):
        for jid, code, body, expected in [("ok", 0, "Delivered", "done"),
                                          ("error", 7, "Error", "failed"),
                                          ("blocked", 0, "STATUS: BLOCKED", "blocked")]:
            with self.subTest(status=expected):
                self.submit(jid, "--prompt-text", "original " + jid, "--tick")
                original_prompt = (self.root / "queue" / (jid + ".prompt.md")).read_text()
                worker = core.remote_dir(jid)
                (worker / "exit").write_text(str(code))
                (worker / "conclusion.md").write_text(body)
                self.cli("status", jid, "--json")
                job = core.find_job(self.root, jid)
                self.assertEqual(job["status"], expected)
                self.assertEqual(job["prompt_file"], str(worker / "prompt.md"))
                self.assertEqual(Path(job["prompt_file"]).read_text(), original_prompt)
                self.assertFalse((self.root / "queue" / (jid + ".prompt.md")).exists())

    def test_cancel_archives_queued_and_running_prompts(self):
        for jid, flags in [("queued", []), ("running", ["--tick"])]:
            with self.subTest(folder=jid):
                self.submit(jid, "--prompt-text", "cancel " + jid, *flags)
                original_prompt = (self.root / "queue" / (jid + ".prompt.md")).read_text()
                self.cli("cancel", jid, "--json")
                job = core.find_job(self.root, jid)
                self.assertEqual(job["status"], "cancelled")
                self.assertEqual(job["prompt_file"], str(core.remote_dir(jid) / "prompt.md"))
                self.assertEqual(Path(job["prompt_file"]).read_text(), original_prompt)
                self.assertFalse((self.root / "queue" / (jid + ".prompt.md")).exists())

    def cancel_process_tree(self, ignore_term):
        ready = self.base / "ready"
        script = self.base / "worker.sh"
        script.write_text("#!/bin/bash\n" + ("trap '' TERM\n" if ignore_term else "") +
                          'if [ "${1:-}" = child ]; then\n'
                          '  sleep 60 &\n'
                          '  printf "%s %s\\n" "$$" "$!" > ' + shlex.quote(str(ready)) + '\n'
                          '  wait\nelse\n  bash "$0" child &\n  wait\nfi\n')
        worker = subprocess.Popen(["bash", str(script)], start_new_session=True)

        def cleanup():
            try:
                os.killpg(worker.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            worker.wait(timeout=5)

        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not ready.exists() or not ready.read_text().strip():
            self.assertLess(time.monotonic(), deadline, "fake worker did not become ready")
            time.sleep(0.01)
        descendants = [int(pid) for pid in ready.read_text().split()]
        self.assertEqual(len(descendants), 2)
        expected_pids = sorted([worker.pid] + descendants)
        # Exercise legacy pid fallback as well as explicit pgid records.
        fields = {"pgid": worker.pid} if ignore_term else {}
        self.record("tree", "running", status="running", pid=worker.pid, **fields)
        started = time.monotonic()
        with patch.dict(os.environ, {"CS_CANCEL_GRACE": "0.2"}):
            result = json.loads(self.cli("cancel", "tree", "--json"))["job"]
        elapsed = time.monotonic() - started
        worker.wait(timeout=5)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(sorted(result["cancel_killed_pids"]), expected_pids)
        self.assertEqual(result["cancel_signal"], "SIGKILL" if ignore_term else "SIGTERM")
        if ignore_term:
            self.assertGreaterEqual(elapsed, 0.2)
        stored = core.find_job(self.root, "tree")
        self.assertEqual(stored["cancel_killed_pids"], result["cancel_killed_pids"])
        self.assertEqual(stored["cancel_signal"], result["cancel_signal"])
        # Zombies are dead too, even when the host's init has not reaped them yet.
        self.assertFalse([pid for pid, state in core.process_group_members(worker.pid)
                          if not state.startswith("Z")])
        for pid in expected_pids:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                                   capture_output=True, text=True, timeout=5).stdout.strip()
            self.assertTrue(not state or state.startswith("Z"), (pid, state))

    def test_cancel_terminates_child_and_grandchild(self):
        self.cancel_process_tree(ignore_term=False)

    def test_cancel_escalates_term_ignoring_tree_to_sigkill(self):
        self.cancel_process_tree(ignore_term=True)

    def test_cancel_already_dead_group(self):
        worker = subprocess.Popen(["bash", "-c", "exit 0"], start_new_session=True)
        worker.wait(timeout=5)
        self.record("dead", "running", status="running", pid=worker.pid, pgid=worker.pid)
        job = json.loads(self.cli("cancel", "dead", "--json"))["job"]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["cancel_killed_pids"], [])
        self.assertIsNone(job["cancel_signal"])

    def test_cancel_permission_error_preserves_running_job(self):
        self.record("denied", "running", status="running", pid=123, pgid=123)
        with patch.object(core, "process_group_members", return_value=[(123, "S")]), \
                patch.object(core.os, "killpg", side_effect=PermissionError("denied")):
            result = json.loads(self.cli("cancel", "denied", "--json", expected=2))
        self.assertIn("denied", result["error"])
        self.assertEqual(core.find_job(self.root, "denied")["status"], "running")
        self.assertFalse((self.root / "done/denied.json").exists())

    def test_cancel_surviving_sigkill_preserves_running_job(self):
        self.record("stuck", "running", status="running", pid=123, pgid=123)
        with patch.dict(os.environ, {"CS_CANCEL_GRACE": "0"}), \
                patch.object(core, "process_group_members", return_value=[(123, "S")]), \
                patch.object(core.os, "killpg") as killpg, \
                patch.object(core.time, "monotonic", side_effect=[0, 1, 1, 7]):
            result = json.loads(self.cli("cancel", "stuck", "--json", expected=2))
        self.assertIn("remains alive after SIGKILL", result["error"])
        self.assertIn(unittest.mock.call(123, signal.SIGTERM), killpg.call_args_list)
        self.assertIn(unittest.mock.call(123, signal.SIGKILL), killpg.call_args_list)
        self.assertEqual(core.find_job(self.root, "stuck")["status"], "running")
        self.assertFalse((self.root / "done/stuck.json").exists())

    def test_status_read_only_preserves_all_state_and_default_still_reclaims(self):
        self.submit("finished", "--prompt-text", "goal", "--tick")
        self.record("live", "running", status="running", pid=os.getpid())
        (self.root / ".lock").unlink()

        def snapshot():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob("*") if p.is_file()}

        before = snapshot()
        for flag in ("--no-reclaim", "--read-only"):
            data = json.loads(self.cli("status", flag, "--json"))
            self.assertEqual(data["reclaimed"], 0)
            self.assertEqual(len(data["running"]), 2)
            job = json.loads(self.cli("status", "finished", flag, "--json"))["job"]
            self.assertEqual(job["status"], "running")
            self.assertEqual(snapshot(), before)
        job = json.loads(self.cli("status", "finished", "--json"))["job"]
        self.assertEqual(job["status"], "done")
        self.assertNotEqual(snapshot(), before)

    def test_status_read_only_does_not_initialize_missing_home(self):
        missing = self.base / "missing"
        with patch.dict(os.environ, {"CS_HOME": str(missing)}):
            self.assertEqual(json.loads(self.cli("status", "--read-only", "--json"))["running"], [])
            self.cli("status", "absent", "--no-reclaim", "--json", expected=1)
        self.assertFalse(missing.exists())

    def test_research_and_explicit_read_only_do_not_require_code_delivery(self):
        text = "分析完成。未提交代码，未推送，未开 PR；只读研究未启动子 agent。"
        for jid, area, flags in [("research", "research", ["--read-only"]),
                                 ("read-only", "review", ["--read-only", "--files", "review.md"])]:
            self.submit(jid, "--area", area, "--prompt-text", "analysis", "--tick", *flags)
            conclusion = core.remote_dir(jid) / "conclusion.md"
            conclusion.write_text(text)
            result = json.loads(self.cli("wait", jid, "--timeout", "0"))["jobs"][0]
            self.assertEqual(result["status"], "done")
            self.assertEqual(result["conclusion"], [text])
        for code, body, expected in [(7, text, "failed"), (124, text, "unknown"), (0, "", "unknown")]:
            conclusion.write_text(body)
            self.assertEqual(core.classify_outcome(code, conclusion, {"read_only": True})[0], expected)

    def test_worker_conclusion_is_external_and_prompt_overrides_template_path(self):
        self.submit("external", "--prompt-text", "Write conclusion.md in the repository.", "--tick")
        jd = core.remote_dir("external")
        self.assertEqual(core.find_job(self.root, "external")["conclusion"], str(jd / "conclusion.md"))
        prompt = (jd / "prompt.md").read_text()
        self.assertIn("overrides template paths", prompt)
        self.assertIn(str(jd / "conclusion.md"), prompt)
        self.assertIn("do not create, stage, or commit", prompt)
        self.assertEqual(list(self.cwd.glob("conclusion*")), [])
        self.assertEqual(json.loads(self.cli("wait", "external"))["jobs"][0]["status"], "done")

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

    def test_wait_tick_advances_queued_jobs_but_default_does_not(self):
        self.submit("one", "--prompt-text", "task", "--files", "shared.py")
        self.submit("two", "--prompt-text", "task", "--files", "shared.py")
        result = json.loads(self.cli("wait", "one", "--timeout", "0", expected=124))
        self.assertEqual(result["jobs"][0]["status"], "queued")
        with patch("time.sleep"):
            result = json.loads(self.cli("wait", "one", "two", "--tick", "--timeout", "5"))
        self.assertFalse(result["timed_out"])
        self.assertEqual([j["status"] for j in result["jobs"]], ["done", "done"])

    def test_wait_tick_timeout_and_missing_job_do_not_launch_unrelated_work(self):
        self.submit("queued", "--prompt-text", "task", "--pin", "codex")
        (self.root / "quota.json").write_text('{"codex": 0}')
        result = json.loads(self.cli("wait", "queued", "--tick", "--timeout", "0", expected=124))
        self.assertEqual(result["jobs"][0]["queue_reasons"][0]["type"], "quota_exhausted")
        (self.root / "quota.json").unlink()
        with patch.object(core, "launch") as launch:
            self.cli("wait", "missing", "--tick", expected=1)
        launch.assert_not_called()

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
        stored = core.find_job(self.root, "git")
        self.assertEqual(stored["changed_files"], ["a.txt"])
        self.assertEqual(stored["scope"]["out_of_scope"], ["a.txt"])
        (self.cwd / "later.txt").write_text("after completion")
        self.assertEqual(json.loads(self.cli("status", "git", "--json"))["job"]["changed_files"], ["a.txt"])

    def test_completion_scope_untracked_protected_and_own_shared(self):
        def git(*args):
            return subprocess.run(["git", "-C", str(self.cwd), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git("init")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--allow-empty", "-m", "base")
        self.submit("scope", "--area", "backend", "--files", str(self.cwd / "src/x.py"),
                    "--lock-shared", "own.py", "--prompt-text", "goal", "--tick")
        self.record("other", "running", status="running", pid=os.getpid(), shared_files=["dist"])
        for name in ("src/x.py", "tests/test_x.py", "dist/x.js", "own.py", "extra.py"):
            path = self.cwd / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("new")
        git("add", "src/x.py")
        job = json.loads(self.cli("wait", "scope"))["jobs"][0]
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["changed_files"], ["dist/x.js", "extra.py", "own.py", "src/x.py", "tests/test_x.py"])
        self.assertEqual(job["scope"], dict(declared=["own.py", "src/x.py"], companion=["tests/test_x.py"],
                                          out_of_scope=["dist/x.js", "extra.py"], shared_violations=["dist/x.js"]))
        stored = core.find_job(self.root, "scope")
        stored["cwd"] = str(self.cwd / "src")
        core.completion_scope(stored, [core.find_job(self.root, "other")])
        self.assertEqual(stored["changed_files"], ["x.py"])
        core.completion_scope(dict(stored, cwd=str(self.base)), [])  # non-Git must not crash

    def test_protected_scope_maps_worktrees_and_ignores_other_repositories(self):
        def git(directory, *args):
            return subprocess.run(["git", "-C", str(directory), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git(self.cwd, "init")
        git(self.cwd, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
            "commit", "--allow-empty", "-m", "base")
        sibling = self.base / "sibling"
        git(self.cwd, "worktree", "add", "-b", "sibling", str(sibling))
        other = self.base / "unrelated"
        other.mkdir()
        git(other, "init")
        running = [dict(id="sibling", cwd=str(sibling), shared_files=["src/core.py", "dist"]),
                   dict(id="unrelated", cwd=str(other), shared_files=["other.py"]),
                   dict(id="self", cwd=str(self.cwd), shared_files=["own.py"])]
        job = dict(id="self", cwd=str(self.cwd))
        self.assertEqual(core.protected_scope_paths(job, running), ["src/core.py", "dist"])
        subdir = self.cwd / "src"
        subdir.mkdir()
        self.assertEqual(core.protected_scope_paths(dict(job, cwd=str(subdir)), running),
                         ["core.py", "../dist"])
        running[0]["shared_files"] = ["."]
        self.assertEqual(core.protected_scope_paths(dict(job, cwd=str(subdir)), running), ["."])

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
            self.assertEqual(core.find_job(self.root, jid)["pgid"], 123)
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
            if pool == "codex":
                executable.write_text('#!/bin/sh\nwhile [ "$#" -gt 0 ]; do\n'
                                      '  if [ "$1" = "-o" ]; then shift; result_path=$1; fi\n'
                                      '  shift\ndone\ncat >/dev/null\nprintf "Delivered\\n" > "$result_path"\n')
            else:
                executable.write_text('#!/bin/sh\nprintf "Delivered\\n"\n')
            result = subprocess.run(["/bin/bash", str(script)], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((core.remote_dir(jid) / "conclusion.md").read_text(), "Delivered\n")
            self.assertEqual(list(self.cwd.glob("conclusion*")), [])

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

    def test_doctor_trust_requires_exact_accepted_project_and_is_only_advisory(self):
        trust = self.base / ".claude.json"
        for config, expected in [
                ({"projects": {str(self.cwd): {"hasTrustDialogAccepted": True}}}, "OK"),
                ({"projects": {str(self.cwd): {"hasTrustDialogAccepted": False}}}, "WARN"),
                ({"projects": {str(self.cwd) + "-other": {"hasTrustDialogAccepted": True}}}, "WARN"),
                ({"projects": {str(self.cwd.parent): {"hasTrustDialogAccepted": True}}}, "WARN"),
                ({"history": str(self.cwd)}, "WARN"), ([], "WARN")]:
            trust.write_text(json.dumps(config))
            with patch.object(Path, "home", return_value=self.base), \
                    patch("socket.getaddrinfo", return_value=[]):
                checks = core.doctor_checks(str(self.cwd))
            hint = next(c for c in checks if c["name"] == "claude_trusted_dir_hint")
            self.assertEqual(hint["level"], expected)
        trust.write_text("invalid JSON")
        with patch.object(Path, "home", return_value=self.base), \
                patch("socket.getaddrinfo", return_value=[]):
            checks = core.doctor_checks(str(self.cwd))
        self.assertEqual(next(c for c in checks if c["name"] == "claude_trusted_dir_hint")["level"], "WARN")

    def test_invalid_identifiers_and_wait_limits_return_schema_errors(self):
        for args in [("status", "../bad"), ("wait", "one", "--timeout", "-1"),
                     ("wait", "one", "--timeout", "nan"), ("status", "one", "--lines", "-1")]:
            self.assertIn("error", json.loads(self.cli(*args, "--json", expected=2)))


if __name__ == "__main__":
    unittest.main()
