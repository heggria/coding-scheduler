"""Handoff evidence contracts; isolated state only, no model/service calls."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
from handoff import build_handoff, render_handoff

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_handoff_cli")


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "state"
        self.env = patch.dict(os.environ, {"CS_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conclusion = self.base / "conclusion.md"
        self.log = self.base / "worker log"
        self.job = {"id": "fixture", "status": "done", "exit": 0,
                    "conclusion": str(self.conclusion), "log": str(self.log),
                    "cwd": str(self.base), "changed_files": ["lib/example.py"]}

    def summary(self, conclusion="STATUS: DONE\n", **fields):
        self.conclusion.write_text(conclusion)
        return core.job_summary(dict(self.job, **fields), lines=0)

    def cli(self, *args, expected=0):
        output = io.StringIO()
        with patch.object(sys, "argv", ["cs", *args]), contextlib.redirect_stdout(output):
            code = CLI["main"]()
        self.assertEqual(code, expected, output.getvalue())
        return output.getvalue()

    def test_done_is_not_acceptance_and_changed_is_not_exclusive(self):
        summary = self.summary()
        card = summary["handoff"]
        self.assertEqual(summary["conclusion"], [])
        self.assertEqual(summary["changed_files"], ["lib/example.py"])
        self.assertEqual(card["scheduler_status"], "done")
        self.assertEqual(card["worker_exit"], 0)
        self.assertEqual(card["acceptance"]["status"], "unproven")
        self.assertEqual(card["verification"][0]["status"], "unknown")
        self.assertEqual(card["attribution"], "shared_cwd_or_unknown")
        self.assertEqual(card["changed"]["attribution"], card["attribution"])
        rendered = render_handoff(card, "fixture")
        self.assertIn("验收未证明", rendered)
        self.assertNotIn("验收通过", rendered)

    def test_full_conclusion_extracts_results_and_risks_beyond_preview(self):
        card = self.summary("""## Summary
Added handoff cards.
## Verification
- `python3 -m pytest tests_cs/test_handoff.py -q`: PASS exit=0
- `npm run build`: failed exit=2
- `pnpm test:browser`: 未运行
- `python3 -m unittest`: result unavailable
## Risks
Real service not verified.
## Next steps
- `npm run test:integration`: should pass
STATUS: DONE
""")["handoff"]
        self.assertEqual(card["summary"], "Added handoff cards.")
        self.assertEqual([item["status"] for item in card["verification"]],
                         ["pass", "fail", "not_run", "unknown"])
        self.assertEqual(card["verification"][1]["exit"], 2)
        self.assertEqual(card["verification"][0]["source"], str(self.conclusion) + "#L4")
        self.assertIn("Real service not verified.", card["risks"])
        self.assertEqual(card["acceptance"]["status"], "unproven")

    def test_structured_records_and_exit_override_status(self):
        card = self.summary(
            'VERIFICATION: {"command":"check one", "status":"pass", "exit":3, "log":"/tmp/check.log"}\n',
            verification=[{"command": "check two", "exit": 0}, {"command": "check three", "status": "not_run"}]
        )["handoff"]
        self.assertEqual([item["status"] for item in card["verification"]], ["pass", "not_run", "fail"])
        self.assertEqual(card["verification"][2]["log"], "/tmp/check.log")
        self.assertEqual(card["verification"][0]["source"], "job.verification")

    def test_log_only_accepts_explicit_records_in_bounded_tail(self):
        self.log.write_text("x" * 70000 + '\n{"text":"tests passed"}\nworker exit=0\n'
                            'VERIFICATION: {"command":"check", "exit":0}\n')
        card = self.summary()["handoff"]
        self.assertEqual(len(card["verification"]), 1)
        self.assertEqual(card["verification"][0]["status"], "pass")
        self.assertEqual(card["verification"][0]["log"], str(self.log))
        self.assertIn("tail", card["verification"][0]["source"])
        self.log.write_text('all tests passed\n{"text":"VERIFICATION: {}"}\n')
        self.assertEqual(self.summary()["handoff"]["verification"][0]["status"], "unknown")

    def test_missing_legacy_and_malformed_fields_stay_unknown(self):
        for job in ({"id": "old"}, dict(self.job, conclusion="/missing/conclusion")):
            card = core.job_summary(job)["handoff"]
            self.assertIn("unknown", card["summary"])
            self.assertEqual(card["verification"][0]["status"], "unknown")
        card = self.summary('VERIFICATION: {bad json}\n',
                            verification=[None, {"status": "green", "exit": "0"}, {"exit": False}])["handoff"]
        self.assertTrue(all(item["status"] == "unknown" for item in card["verification"]))
        self.assertEqual(self.summary("Old conclusion without any fields.")["handoff"]["summary"],
                         "Old conclusion without any fields.")

    def test_planned_ambiguous_and_negative_results_do_not_pass(self):
        for line, expected in [
            ("验证: `pytest` not passed", "fail"),
            ("验证: `pytest` 没有通过", "fail"),
            ("验证: `pytest` 未执行，预计通过", "not_run"),
            ("验证: `pytest` should pass", "unknown"),
            ("验证: `pytest` and `npm test` passed", "unknown"),
            ("下一步: `pytest` passed", "unknown"),
            ("STATUS: DONE\nall tests passed", "unknown")]:
            with self.subTest(line=line):
                checks = self.summary(line)["handoff"]["verification"]
                self.assertTrue(all(item["status"] == expected for item in checks))

    def test_fenced_commands_and_worker_failure_are_not_test_results(self):
        card = self.summary('## Verification\n```sh\npytest\n'
                            'VERIFICATION: {"command":"example only", "exit":0}\n```\n',
                            status="failed", exit=1)["handoff"]
        self.assertEqual(card["scheduler_status"], "failed")
        self.assertEqual(card["verification"][0]["status"], "unknown")
        self.assertEqual(card["acceptance"]["status"], "unproven")

    def test_summary_can_mention_tests_without_becoming_evidence(self):
        card = self.summary("Added handoff tests.\nSTATUS: DONE\n")["handoff"]
        self.assertEqual(card["summary"], "Added handoff tests.")
        self.assertEqual(card["verification"][0]["status"], "unknown")

    def test_suggestions_are_shell_quoted_and_not_executed(self):
        job = dict(self.job, cwd="/tmp/a 'b;$(touch nope)", base_sha="a" * 40)
        summary = {"status": "done", "artifacts": {"conclusion": "/tmp/a 'b", "log": str(self.log)}}
        card = build_handoff(job, summary)
        self.assertEqual(shlex.split(card["next_steps"][1]), ["cat", "--", "/tmp/a 'b"])
        self.assertEqual(shlex.split(card["next_steps"][-1]),
                         ["git", "-C", job["cwd"], "diff", job["base_sha"], "--", "."])

    def test_display_is_bounded_but_json_keeps_all_evidence(self):
        card = self.summary("Added cards.\n", verification=[
            {"command": "check %d\n%s" % (i, "x" * 1000), "status": "pass"} for i in range(20)
        ])["handoff"]
        rendered = render_handoff(card, "fixture")
        self.assertEqual(len(card["verification"]), 20)
        self.assertLessEqual(len(rendered.splitlines()), 18)
        self.assertIn("另 16 项", rendered)

    def test_cli_status_wait_json_text_and_timeout(self):
        self.summary("Added cards.\n验证: `pytest` passed\n")
        core.ensure(self.root)
        core.write_job(self.root, "done", self.job)
        status = json.loads(self.cli("status", "fixture", "--no-reclaim", "--json"))
        waited = json.loads(self.cli("wait", "fixture", "--timeout", "0"))
        self.assertEqual(status["schema_version"], "1")
        self.assertEqual(status["job"]["handoff"], waited["jobs"][0]["handoff"])
        self.assertIn("接手卡 fixture", self.cli("status", "fixture", "--no-reclaim"))
        self.assertIn("接手卡 fixture", self.cli("wait", "fixture", "--text"))
        self.assertEqual(json.loads(self.cli("--json", "wait", "fixture", "--text"))["schema_version"], "1")
        core.write_job(self.root, "queue", dict(self.job, id="queued", status="queued"))
        data = json.loads(self.cli("status", "--no-reclaim", "--json"))
        self.assertNotIn("handoff", data["queued"][0])
        self.assertIn("queued", self.cli("status", "--no-reclaim"))
        data = json.loads(self.cli("wait", "queued", "--timeout", "0", expected=124))
        self.assertEqual(data["jobs"][0]["handoff"]["scheduler_status"], "queued")
        self.assertEqual(core.find_job(self.root, "queued")["status"], "queued")


if __name__ == "__main__":
    unittest.main()
