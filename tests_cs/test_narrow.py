"""Narrow hints are observations; retries carry evidence without granting scope."""
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
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
from narrow import NARROW_CLASSES, candidate_paths, candidate_warnings, narrow_class, retry_delta

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_narrow_cli")


class NarrowTests(unittest.TestCase):
    def test_subcauses_and_precedence(self):
        base = dict(files=["lib/x.py"], need_files=["tests/test_x.py"], scope_policy="companion")
        cases = [({}, "", "companion_miss"),
                 ({"need_files": ["lib/business.py"]}, "", "companion_miss"),
                 ({"scope_policy": "strict"}, "", "strict_companion_known"),
                 ({"protected_paths": ["tests"]}, "", "shared_or_core"),
                 ({"scope": {"shared_violations": ["tests/test_x.py"]}}, "", "shared_or_core"),
                 ({}, "未声明的核心文件受限", "shared_or_core"),
                 ({}, "EACCES: permission denied", "permission_or_lock"),
                 ({"read_only": True}, "", "permission_or_lock"),
                 ({}, "lock held by another job", "permission_or_lock"),
                 ({}, "输入不清，待澄清", "unclear_input"),
                 ({"need_files": []}, "", "unclear_input"),
                 ({"need_files": ["lib/x.py"]}, "", "unclear_input"),
                 ({"scope_policy": "strict", "need_files": ["lib/business.py"]}, "", "other"),
                 ({"scope_policy": "flex", "need_files": ["lib/business.py"]}, "", "other")]
        seen = set()
        for extra, text, expected in cases:
            with self.subTest(extra=extra, text=text):
                actual = narrow_class(dict(base, **extra), text)
                self.assertEqual(actual, expected)
                seen.add(actual)
        self.assertEqual(seen, set(NARROW_CLASSES))
        self.assertEqual(narrow_class(dict(base, scope_policy="strict"),
                                     "shared/core protections still apply"), "strict_companion_known")
        self.assertEqual(narrow_class(dict(files=["lib/x.py"]),
                                     "NEED_FILES: tests/test_x.py"), "strict_companion_known")

    def test_candidates_are_concrete_explainable_and_do_not_change_scope(self):
        files = ["lib/x.py", "web/package.json"]
        known = ["tests/test_x.py", "tests/test_other.py", "web/yarn.lock", "lib/__init__.py",
                 "lib/x.py", "dist/unrelated.js", "other/__init__.py"]
        candidates = candidate_paths(files, known)
        self.assertEqual({c["path"] for c in candidates},
                         {"tests/test_x.py", "web/yarn.lock", "lib/__init__.py"})
        self.assertTrue(all(c["reason"] for c in candidates))
        job = dict(id="mine", cwd="/tmp/narrow-work", files=files,
                   shared_files=[], scope_policy="companion")
        others = [dict(id="busy", cwd=job["cwd"], files=["tests/test_x.py"], shared_files=[])]
        before = json.dumps(job, sort_keys=True)
        warnings = candidate_warnings(job, others, known)
        self.assertTrue(any("tests/test_x.py" in w and "busy (write lock" in w for w in warnings))
        self.assertTrue(all("hint only, --files unchanged" in w for w in warnings))
        self.assertEqual(json.dumps(job, sort_keys=True), before)
        self.assertEqual(files, ["lib/x.py", "web/package.json"])

    def test_delta_keeps_bounded_evidence_and_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "conclusion.md"
            path.write_text("新增决定：保留接口\n" + "x" * 20000 + "\n验证：pytest failed\n")
            original = dict(id="attempt", status="blocked", conclusion=str(path),
                            user_prompt="original Brief Decisions", run_delta=[{"job_id": str(i)} for i in range(8)])
            delta = retry_delta(original, "已试 A 失败，改用 B")
            self.assertEqual(len(delta), 5)
            self.assertEqual(delta[0]["job_id"], "4")
            record = delta[-1]
            self.assertEqual(record["conclusion_path"], str(path))
            self.assertTrue(record["truncated"])
            self.assertIn("新增决定", record["conclusion_excerpt"])
            self.assertIn("pytest failed", record["conclusion_excerpt"])
            self.assertEqual(record["operator_note"], "已试 A 失败，改用 B")
            self.assertNotIn("original Brief", json.dumps(delta))
            path.unlink()
            self.assertTrue(retry_delta(original)[-1]["conclusion_unavailable"])


class NarrowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = Path(self.temp.name).resolve() / "work"
        self.cwd.mkdir()
        self.root = self.cwd.parent / "state"
        env = patch.dict(os.environ, CS_HOME=str(self.root), CS_TEST_FAKE_WORKER="1")
        env.start()
        self.addCleanup(env.stop)
        nested = patch.dict(os.environ)
        nested.start()
        self.addCleanup(nested.stop)
        os.environ.pop("CS_JOB_ID", None)
        core.ensure()
        subprocess.run(["git", "init", "-q", str(self.cwd)], check=True)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with patch.object(sys, "argv", ["cs", *args]), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(CLI["main"](), 0, err.getvalue())
        return json.loads(out.getvalue()), err.getvalue()

    def submit(self, jid="original", *extra):
        return self.cli("submit", "--project", "fixture", "--area", "backend", "--tier", "M",
                        "--cwd", str(self.cwd), "--files", "lib/x.py", "--id", jid,
                        "--prompt-text", "original Brief Decisions", "--no-tick", "--json", *extra)

    def test_submit_warns_on_candidate_conflict_without_expansion_or_new_locks(self):
        (self.cwd / "tests").mkdir()
        (self.cwd / "tests/test_x.py").write_text("test")
        core.write_job(self.root, "queue", dict(id="busy", cwd=str(self.cwd), files=["tests/test_x.py"],
                                               shared_files=[], status="queued"))
        result, stderr = self.submit()
        job = core.find_job(self.root, result["id"])
        self.assertEqual(job["files"], ["lib/x.py"])
        self.assertEqual(job["shared_files"], [])
        self.assertEqual([l["path"] for l in job["lock_ctx"]["locks"]], ["lib/x.py"])
        warning = next(w for w in result["warnings"] if "candidate tests/test_x.py" in w)
        self.assertIn("busy (write lock", warning)
        self.assertIn(warning, stderr)

    def test_reclaim_retry_and_board_trace_execution_delta(self):
        self.submit("original", "--tick")
        worker = core.remote_dir("original")
        text = ("新增决定：保留接口\n已试失败：A 路径不可用\n验证：pytest failed\n"
                "NEED_FILES: tests/test_x.py\nSTATUS: BLOCKED\n")
        (worker / "conclusion.md").write_text(text)
        (worker / "exit").write_text("0")
        self.assertEqual(core.reclaim(self.root), 1)
        original = core.find_job(self.root, "original")
        self.assertEqual(original["narrow_reason_class"], "companion_miss")
        before = (self.root / "done/original.json").read_bytes()
        result, _ = self.cli("retry", "original", "--run-delta", "输入已澄清", "--json")
        retry = core.find_job(self.root, result["id"])
        self.assertEqual(retry["files"], original["files"])
        self.assertEqual(retry["shared_files"], original["shared_files"])
        self.assertEqual(retry["user_prompt"], "original Brief Decisions")
        self.assertEqual(retry["run_delta"][-1]["conclusion_excerpt"], text)
        self.assertEqual(retry["run_delta"][-1]["operator_note"], "输入已澄清")
        self.assertEqual(result["job"]["run_delta"], retry["run_delta"])
        prompt = Path(retry["prompt_file"]).read_text()
        self.assertEqual(prompt.count("original Brief Decisions"), 1)
        self.assertIn("已试失败", prompt)
        self.assertIn(str(worker / "conclusion.md"), prompt)
        self.assertEqual((self.root / "done/original.json").read_bytes(), before)
        self.assertIn("companion_miss=1", core.write_board(self.root).read_text())
        automatic = core.find_job(self.root, core.retry_transient(
            self.root, dict(original, status="failed", failure_reason="upstream_transient")))
        self.assertIn("pytest failed", Path(automatic["prompt_file"]).read_text())
        self.assertEqual(automatic["run_delta"][-1]["job_id"], "original")

    def test_candidate_shared_conflict_across_worktrees_is_hint_only(self):
        # Inject the same repository identity without creating real worktrees.
        job = dict(id="mine", cwd=str(self.cwd), files=["x.py"], shared_files=[], scope_policy="strict")
        other = dict(id="sibling", cwd=str(self.cwd.parent / "sibling"), files=[], shared_files=["tests/test_x.py"])
        with patch("locks.GitLockResolver.__call__", side_effect=lambda cwd: ("repo", str(cwd))):
            warnings = candidate_warnings(job, [other], ["tests/test_x.py"])
        self.assertIn("sibling (shared lock", warnings[0])
        self.assertEqual(job["shared_files"], [])


if __name__ == "__main__":
    unittest.main()
