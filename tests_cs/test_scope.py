"""Scope policies are lexical observations, not enforcement or new locks."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from scope import companion_patterns, is_companion, narrow_warnings, scope_report


class ScopeTests(unittest.TestCase):
    def test_companions(self):
        files = ["src/feature/widget.py", "ui/feature/card.tsx", "web/package.json",
                 "pyproject.toml", "rust/Cargo.toml", "go/go.mod"]
        for path in ["tests/test_widget.py", "tests/unit/test_widget_extra.py",
                     "app/tests/deep/test_widget.py", "src/widget_test.py",
                     "ui/feature/card.test.tsx", "ui/feature/card.spec.ts",
                     "ui/feature/e2e/card.cy.ts", "ui/feature/e2e/nested/card.spec.js",
                     "fixtures/data.json", "tests/fixtures/data.json", "testdata/data.json",
                     "tests/__snapshots__/result", "result.snap", "src/__init__.py",
                     "src/feature/__init__.py", "__init__.py", "web/package-lock.json",
                     "web/pnpm-lock.yaml", "web/yarn.lock", "uv.lock", "poetry.lock",
                     "rust/Cargo.lock", "go/go.sum", "dist/a.js", "app/build/b.css",
                     "web/static/dist/b.css", "assets/a.min.js", "a.js.map",
                     "src/generated/i18n/en.json", "src/i18n/en.generated.json",
                     "generated/locales/en.json", "locales/generated/en.json"]:
            with self.subTest(path=path):
                self.assertTrue(is_companion(path, files))
        for path in ["tests/test_other.py", "other/__init__.py", "package-lock.json",
                     "other/uv.lock", "go.sum", "src/unrelated.py", "src/i18n/en.json",
                     "ui/other/e2e/card.cy.ts", "fixtures.py"]:
            with self.subTest(path=path):
                self.assertFalse(is_companion(path, files))
        self.assertIn("web/package-lock.json", companion_patterns(files))
        self.assertFalse(is_companion("../outside.py", files))

    def test_report_policies_and_protection(self):
        changed = ["./src/x.py", "tests/test_x.py", "dist/x.js", "extra.py", "src/x.py"]
        for policy in ("companion", "flex"):
            report = scope_report(changed, ["src/x.py"], ["dist"], policy)
            self.assertEqual(report, dict(declared=["src/x.py"], companion=["tests/test_x.py"],
                                          out_of_scope=["dist/x.js", "extra.py"],
                                          shared_violations=["dist/x.js"]))
        report = scope_report(changed, ["src/x.py"], ["src/x.py"], "strict")
        self.assertEqual(report["companion"], [])
        self.assertEqual(report["shared_violations"], [])
        self.assertEqual(len(report["out_of_scope"]), 3)
        self.assertEqual(scope_report(["own.lock"], ["own.lock"], ["own.lock"], "flex")["declared"],
                         ["own.lock"])
        self.assertEqual(scope_report(["a"], [], ["."], "flex")["shared_violations"], ["a"])
        with self.assertRaises(ValueError):
            scope_report([], [], [], "typo")

    def test_warning_heuristics(self):
        self.assertEqual(narrow_warnings([]), [])
        self.assertEqual(narrow_warnings(["src/x.py", "tests/test_x.py"]), [])
        self.assertIn("tests/test_x.py", narrow_warnings(["src/x.py"])[0])
        self.assertTrue(narrow_warnings(["package.json"]))
        self.assertEqual(narrow_warnings(["package.json", "yarn.lock"]), [])
        self.assertEqual(narrow_warnings(["web/pyproject.toml", "web/uv.lock"]), [])
        self.assertTrue(any("build output" in warning for warning in narrow_warnings(["src/x.tsx"])))
        self.assertEqual(narrow_warnings(["src/x.tsx", "src/x.test.tsx", "dist/x.js"]), [])
