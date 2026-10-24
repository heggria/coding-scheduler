"""Capability evidence must distinguish unobserved from confirmed available."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from doctor_caps import CAPABILITIES, doctor_capabilities, normalize_capabilities
from advise import advise

CLI = runpy.run_path(str(REPO / "bin/cs"), run_name="cs_caps_cli")
BRIEF = """Goal: Implement export
Current: CSV only
Desired: JSON too
Out of scope: No migration
Acceptance Criteria:
1. CSV keeps headers
2. JSON returns an array
Verify: python3 -m unittest discover
Files: lib/export.py,tests/test_export.py
Decisions: Keep API
Unknowns: 无
MR: 禁止
"""


class CapabilityTests(unittest.TestCase):
    def test_unknown_is_not_ok(self):
        caps = doctor_capabilities()
        self.assertEqual(set(caps), set(CAPABILITIES))
        self.assertTrue(all(value["status"] == "unknown" for value in caps.values()))
        self.assertEqual(normalize_capabilities({"auth": {"status": "ok"}})["auth"]["status"], "ok")
        with self.assertRaises(ValueError):
            normalize_capabilities({"auth": {"status": "available"}})

    def test_reuses_known_checks_and_selected_pool(self):
        checks = [{"name": name, "ok": ok, "detail": name} for name, ok in
                  (("codex_cli", True), ("claude_cli", False), ("cwd_exists", True),
                   ("git_writable", True), ("github_dns", False))]
        caps = doctor_capabilities(checks, "codex")
        for name in ("cli_pool_ready", "cwd_git_writable", "resume_supported"):
            self.assertEqual(caps[name]["status"], "ok")
        self.assertEqual(caps["network_dns"]["status"], "unavailable")
        self.assertEqual(caps["browser_or_mcp"]["status"], "unknown")
        self.assertEqual(caps["auth"]["status"], "unknown")
        self.assertEqual(caps["network_access"]["status"], "unknown")
        self.assertEqual(doctor_capabilities(checks)["cli_pool_ready"]["status"], "unknown")
        self.assertEqual(doctor_capabilities(checks, "cc")["cli_pool_ready"]["status"], "unavailable")
        self.assertEqual(doctor_capabilities(checks, "opencode")["resume_supported"]["status"], "unavailable")

    def test_failed_cwd_and_non_git_are_not_available(self):
        for name in ("cwd_exists", "git_repo", "git_writable"):
            caps = doctor_capabilities([{"name": name, "ok": False, "detail": "failed"}])
            self.assertEqual(caps["cwd_git_writable"]["status"], "unavailable")
        self.assertEqual(doctor_capabilities([{"name": "cwd_exists", "ok": True}])
                         ["cwd_git_writable"]["status"], "unknown")
        self.assertEqual(doctor_capabilities([{"name": "git_writable", "ok": False,
                                               "level": "WARN", "detail": "git timed out"}])
                         ["cwd_git_writable"]["status"], "unknown")

    def test_required_capabilities_change_advice_without_permanent_labels(self):
        with tempfile.TemporaryDirectory() as cwd:
            unknown = advise(cwd, BRIEF)
            self.assertEqual(unknown["recommendation"], "plan")
            self.assertIsNone(unknown["suggested_command"])
            caps = {name: {"status": "ok", "detail": "fixture evidence"} for name in CAPABILITIES}
            self.assertEqual(advise(cwd, BRIEF, capabilities=caps)["recommendation"], "submit")
            for name, requirement in (("browser_or_mcp", "Browser MCP Figma"),
                                      ("auth", "Authentication login"), ("network_dns", "Network DNS"),
                                      ("resume_supported", "resume")):
                prompt = BRIEF + "\nTools: " + requirement
                for status in ("unknown", "unavailable"):
                    evidence = dict(caps, **{name: {"status": status}})
                    result = advise(cwd, prompt, capabilities=evidence)
                    self.assertIn(result["recommendation"], ("inline", "plan"))
                    self.assertIsNone(result["suggested_command"])
                    self.assertTrue(any(name + "=" + status in reason for reason in result["reasons"]))
                self.assertEqual(advise(cwd, prompt, capabilities=caps)["recommendation"], "submit")
            # No browser/auth/network requirement: these may remain unobserved.
            minimal = {name: caps[name] for name in ("cli_pool_ready", "cwd_git_writable")}
            self.assertEqual(advise(cwd, BRIEF, capabilities=minimal)["recommendation"], "submit")

    def test_doctor_to_advise_snapshot_is_read_only_and_checks_context(self):
        with tempfile.TemporaryDirectory() as cwd:
            checks = [{"name": name, "ok": True, "level": "OK", "detail": "fixture"}
                      for name in ("codex_cli", "cwd_exists", "git_writable", "github_dns")]
            out = io.StringIO()
            with patch.dict(CLI["main"].__globals__, ensure=Mock(return_value=Path(cwd)),
                            doctor_checks=Mock(return_value=checks)), \
                    patch.object(sys, "argv", ["cs", "doctor", "--cwd", cwd, "--pool", "codex", "--json"]), \
                    contextlib.redirect_stdout(out):
                self.assertEqual(CLI["main"](), 0)
            report = json.loads(out.getvalue())
            self.assertEqual(report["capabilities"]["auth"]["status"], "unknown")
            path = Path(cwd) / "doctor.json"
            for change, expected in (({}, 0), ({"capabilities": {}}, 0),
                                     ({"pool": "cc"}, 2), ({"cwd": "/other"}, 2),
                                     ({"capabilities": {"auth": {"status": "available"}}}, 2)):
                path.write_text(json.dumps(dict(report, **change)))
                out = io.StringIO()
                forbidden = Mock(side_effect=AssertionError("no probing or scheduling"))
                with patch.dict(CLI["main"].__globals__, ensure=forbidden, doctor_checks=forbidden,
                                tick=forbidden, cmd_submit=forbidden), \
                        patch("subprocess.run", forbidden), patch("socket.getaddrinfo", forbidden), \
                        patch.object(sys, "argv", ["cs", "advise", "--cwd", cwd, "--prompt-text", BRIEF,
                                                   "--doctor-report", str(path), "--json"]), \
                        contextlib.redirect_stdout(out):
                    self.assertEqual(CLI["main"](), expected)
                if expected == 0:
                    self.assertEqual(json.loads(out.getvalue())["recommendation"],
                                     "plan" if change else "submit")
                forbidden.assert_not_called()


if __name__ == "__main__":
    unittest.main()
