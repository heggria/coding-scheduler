"""Advice is a read-only scaffold, never permission to launch a worker."""
import contextlib
import io
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from advise import AREAS, advise, parse_brief

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_advise_cli")

CODING = """Goal: Add a deterministic export API across two modules
Current: Only one export format is available
Desired: Export supports CSV and JSON
Out of scope: No database migration
Acceptance Criteria:
1. CSV export includes the header row
2. JSON export returns an array for an empty result
Verify: python3 -m unittest discover
Files:
- lib/export.py
- tests/test_export.py
Decisions: Keep the existing endpoint signature
Unknowns: 无
MR: 禁止
"""

RESEARCH = """Goal: Trace the export format contract
Scope: lib/export.py and its callers in the current checkout
Deliverables: A conclusion with source paths, evidence and remaining questions
Out of scope: No implementation changes
Acceptance Criteria:
1. List every caller with a source path
2. Cite the empty-result behavior with line numbers
Verify: rg -n export lib
Already tried: 无
Unknowns: 无
Execution: background
"""


class AdviseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name).resolve() / "work"
        self.cwd.mkdir()

    def advice(self, prompt=CODING, **kwargs):
        kwargs.setdefault("capabilities", {
            "cli_pool_ready": {"status": "ok"}, "cwd_git_writable": {"status": "ok"}})
        return advise(str(self.cwd), prompt, **kwargs)

    def test_multifile_ready_and_copyable_command(self):
        result = self.advice()
        self.assertEqual(result["recommendation"], "submit")
        self.assertEqual(result["missing"], [])
        self.assertEqual(set(result), {"recommendation", "reasons", "missing", "brief_draft", "suggested_command",
                                      "capabilities", "required_capabilities"})
        argv = shlex.split(result["suggested_command"])
        self.assertEqual(argv[:2], ["cs", "submit"])
        for flag, value in (("--cwd", str(self.cwd)), ("--files", "lib/export.py,tests/test_export.py"),
                            ("--pin", "codex"), ("--prompt-text", CODING), ("--tier", "M")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--no-tick", argv)
        self.assertNotIn("--tick", argv)
        self.assertNotIn("--force", argv)
        self.assertEqual(result, self.advice())

    def test_research_read_only_allows_empty_files_and_has_typed_dor(self):
        result = self.advice(RESEARCH, area="research", read_only=True)
        self.assertEqual(result["recommendation"], "submit")
        self.assertEqual(result["missing"], [])
        argv = shlex.split(result["suggested_command"])
        self.assertIn("--read-only", argv)
        self.assertNotIn("--files", argv)
        self.assertIn("Scope:", result["brief_draft"])
        self.assertNotIn("Current:", result["brief_draft"])

    def test_only_research_read_only_has_empty_files_exception(self):
        for area, read_only in (("research", False), ("review", True), ("backend", True)):
            with self.subTest(area=area, read_only=read_only):
                result = self.advice(RESEARCH, area=area, read_only=read_only)
                self.assertTrue(any(item.startswith("files:") for item in result["missing"]))
                self.assertIsNone(result["suggested_command"])

    def test_single_file_small_change_is_inline(self):
        result = self.advice("Goal: 修正 README 文案小改", files="README.md")
        self.assertEqual(result["recommendation"], "inline")
        self.assertIsNone(result["suggested_command"])
        self.assertTrue(result["missing"])
        self.assertIn("<待补充", result["brief_draft"])

    def test_missing_or_one_ac_is_not_ready(self):
        for replacement in ("", "Acceptance Criteria:\n1. CSV export includes the header row\n"):
            prompt = CODING.replace("Acceptance Criteria:\n1. CSV export includes the header row\n"
                                    "2. JSON export returns an array for an empty result\n", replacement)
            result = self.advice(prompt)
            self.assertEqual(result["recommendation"], "plan")
            self.assertTrue(any(item.startswith("acceptance_criteria:") for item in result["missing"]))
            self.assertIsNone(result["suggested_command"])

    def test_each_dor_field_is_required(self):
        fields = parse_brief(CODING)
        for key in ("goal", "current", "desired", "out_of_scope", "verify", "decisions", "unknowns", "mr"):
            with self.subTest(key=key):
                prompt = CODING.replace(fields[key].strip(), "<待补充>")
                result = self.advice(prompt)
                self.assertTrue(any(item.startswith(key + ":") for item in result["missing"]))
                self.assertIsNone(result["suggested_command"])
        for key in ("scope", "deliverables"):
            prompt = RESEARCH.replace(parse_brief(RESEARCH)[key].strip(), "<待补充>")
            self.assertTrue(any(item.startswith(key + ":") for item in
                                self.advice(prompt, area="research", read_only=True)["missing"]))

    def test_placeholder_ac_and_duplicate_ac_do_not_count(self):
        for ac in ("1. <可测 AC>\n2. <可测 AC>", "1. 功能正常\n2. 符合预期", "1. CSV has header\n2. CSV has header"):
            prompt = CODING.replace("1. CSV export includes the header row\n2. JSON export returns an array for an empty result", ac)
            self.assertTrue(any(item.startswith("acceptance_criteria:") for item in self.advice(prompt)["missing"]))

    def test_plan_and_tools_override_otherwise_ready_task(self):
        for note, expected in (("先定方案", "plan"), ("方案未定", "plan"),
                               ("Figma / 浏览器点选闭环", "inline"), ("worker 无所需 MCP", "inline"),
                               ("missing required tool", "inline"), ("不用 cs", "inline")):
            with self.subTest(note=note):
                result = self.advice(CODING + "\nConstraints: " + note)
                self.assertEqual(result["recommendation"], expected)
                self.assertIsNone(result["suggested_command"])

    def test_blocking_unknowns_require_plan(self):
        result = self.advice(CODING.replace("Unknowns: 无", "Unknowns: API contract needs clarification"))
        self.assertEqual(result["recommendation"], "plan")
        self.assertIsNone(result["suggested_command"])
        result = self.advice(CODING.replace("Unknowns: 无", "Unknowns: 不阻塞：响应文案尚未确认，沿用现状"))
        self.assertEqual(result["missing"], [])

    def test_files_must_be_specific_and_are_never_invented(self):
        (self.cwd / "src").mkdir()
        for paths in ("src", "missing/", "lib/*.py", ".", "<files>"):
            result = self.advice(files=paths)
            self.assertTrue(any(item.startswith("files:") for item in result["missing"]))
            self.assertIsNone(result["suggested_command"])
        result = self.advice("请查看 lib/export.py，文件范围待讨论")
        self.assertTrue(any(item.startswith("files:") for item in result["missing"]))

    def test_review_uses_scope_and_deliverables_but_requires_files(self):
        result = self.advice(RESEARCH, area="review", read_only=True, files="review.md")
        self.assertEqual(result["recommendation"], "submit")
        self.assertEqual(result["missing"], [])

    def test_tools_and_explicit_background_intent(self):
        result = self.advice(CODING + "\nTools: browser unavailable")
        self.assertEqual(result["recommendation"], "inline")
        prompt = RESEARCH.replace("Execution: background", "Execution: cannot run in background")
        self.assertEqual(self.advice(prompt, area="research", read_only=True)["recommendation"], "inline")
        prompt = RESEARCH.replace("Execution: background", "").replace("Trace the export format contract", "调研后台服务导出契约")
        self.assertEqual(self.advice(prompt, area="research", read_only=True)["recommendation"], "inline")

    def test_metadata_overrides_and_shell_quoting(self):
        prompt = CODING + "\nConstraints: Keep literal 'quotes', $(touch nope), `touch nope`, and $HOME."
        result = self.advice(prompt, project="demo", tier="H", idempotency_key="operator-key",
                             files="lib/a file.py,lib/b'file.py")
        argv = shlex.split(result["suggested_command"])
        self.assertEqual(argv[argv.index("--prompt-text") + 1], prompt)
        self.assertEqual(argv[argv.index("--files") + 1], "lib/a file.py,lib/b'file.py")
        self.assertEqual(argv[argv.index("--idempotency-key") + 1], "operator-key")
        self.assertFalse((self.cwd / "nope").exists())
        different = self.advice(files="a.py,b.py")
        self.assertNotEqual(shlex.split(different["suggested_command"])[-3], "operator-key")

    def test_chinese_markdown_headings_and_read_only_field(self):
        prompt = RESEARCH.replace("Goal:", "## 目标：").replace("Scope:", "## 调研范围：")
        prompt = prompt.replace("Acceptance Criteria:", "**验收标准：**").replace("Unknowns:", "未知项：")
        result = self.advice(prompt + "\nRead-only: true\n\n", area="research")
        self.assertEqual(result["missing"], [])
        self.assertEqual(result["recommendation"], "submit")

    def test_fenced_fields_do_not_treat_language_as_ac_or_path(self):
        prompt = CODING.replace("Files:\n- lib/export.py\n- tests/test_export.py",
                                "Files:\n```text\nlib/export.py\ntests/test_export.py\n```")
        result = self.advice(prompt)
        argv = shlex.split(result["suggested_command"])
        self.assertEqual(argv[argv.index("--files") + 1], "lib/export.py,tests/test_export.py")
        prompt = prompt.replace("1. CSV export includes the header row\n2. JSON export returns an array for an empty result",
                                "```text\n1. CSV export includes the header row\n```")
        result = self.advice(prompt)
        self.assertIsNone(result["suggested_command"])
        self.assertIn("2. <待补充", result["brief_draft"])

    def test_none_is_not_a_verify_command_or_implementation_goal(self):
        for field, original in (("verify", "python3 -m unittest discover"),
                                ("goal", "Add a deterministic export API across two modules")):
            result = self.advice(CODING.replace(original, "无"))
            self.assertTrue(any(item.startswith(field + ":") for item in result["missing"]))
            self.assertIsNone(result["suggested_command"])

    def test_missing_cwd_is_advice_not_an_initialization(self):
        absent = self.cwd / "absent"
        result = advise(str(absent), CODING)
        self.assertTrue(any(item.startswith("cwd:") for item in result["missing"]))
        self.assertIsNone(result["suggested_command"])
        self.assertFalse(absent.exists())

    def test_coding_areas_share_the_same_dor(self):
        for area in set(AREAS) - {"research", "review"}:
            self.assertEqual(self.advice(area=area)["missing"], [])

    def test_cli_never_calls_scheduler_process_or_network(self):
        forbidden = Mock(side_effect=AssertionError("advise must stay read-only"))
        out = io.StringIO()
        with patch.dict(CLI["main"].__globals__, dict(ensure=forbidden, state_lock=forbidden,
                                                    tick=forbidden, cmd_submit=forbidden)), \
                patch("subprocess.run", forbidden), patch("urllib.request.urlopen", forbidden), \
                patch.object(sys, "argv", ["cs", "advise", "--cwd", str(self.cwd), "--prompt-text", CODING, "--json"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(CLI["main"](), 0)
        self.assertEqual(json.loads(out.getvalue())["recommendation"], "plan")
        forbidden.assert_not_called()

    def test_subprocess_has_no_state_repo_or_bytecode_writes(self):
        isolated = self.cwd / "scheduler"
        (isolated / "bin").mkdir(parents=True)
        shutil.copy2(REPO / "bin/cs", isolated / "bin/cs")
        shutil.copytree(REPO / "lib", isolated / "lib", ignore=shutil.ignore_patterns("__pycache__", "dashboard_dist"))
        state = self.cwd / "state"
        env = dict(os.environ, CS_HOME=str(state), CS_JOB_ID="inside-worker")
        def snapshot():
            return {str(p.relative_to(self.cwd)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.cwd.rglob("*") if p.is_file()}
        before = snapshot()
        for prefix, tail in ((["advise"], []), (["--json", "advise"], []), (["advise"], ["--json"])):
            process = subprocess.run([sys.executable, str(isolated / "bin/cs"), *prefix,
                                      "--cwd", str(self.cwd), "--prompt-text", CODING, *tail],
                                     env=env, text=True, capture_output=True, check=True)
            if "--json" in prefix + tail:
                result = json.loads(process.stdout)
                self.assertEqual(result["schema_version"], "1")
                self.assertEqual(result["recommendation"], "plan")
            else:
                self.assertTrue(process.stdout.startswith("recommendation: plan\n"))
                command = process.stdout.split("\nsuggested_command:\n", 1)[1].strip()
                self.assertEqual(command, "null")
        self.assertEqual(snapshot(), before)
        self.assertFalse(state.exists())
        process = subprocess.run([sys.executable, str(isolated / "bin/cs"), "advise", "--help"],
                                 env=env, text=True, capture_output=True, check=True)
        self.assertIn("--prompt-text", process.stdout)
        self.assertNotIn("--force", process.stdout)
        self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
