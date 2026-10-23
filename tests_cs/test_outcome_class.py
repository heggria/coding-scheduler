"""Outcome classes keep operator cancels, external signals and infra errors out of task failures."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
from stats import TZ, aggregate, classify_event, collect_stats, job_event, outcome_class


class OutcomeClassTests(unittest.TestCase):
    def test_classes(self):
        cases = [
            (("done",), "success"),
            (("cancelled",), "operator"),
            (("failed", "signaled", 143), "interrupted"),
            (("failed", None, 130), "interrupted"),
            (("failed", "upstream_transient", 1), "infra"),
            (("failed", "rate_limit", 1), "infra"),
            (("failed", "no_output_timeout", 1), "infra"),
            (("failed", None, 1), "task"),
            (("blocked", None, 0, "files_narrow"), "task"),
            (("blocked", None, 0, "upstream_transient"), "infra"),
            (("unknown",), "task"),
        ]
        for args, expected in cases:
            self.assertEqual(outcome_class(*args), expected, args)

    def test_task_success_rate_excludes_noise(self):
        events = [dict(status=s, outcome_class=c, failure_reason=f, cancel_reason=r) for s, c, f, r in [
            ("done", "success", None, None), ("done", "success", None, None), ("done", "success", None, None),
            ("failed", "task", None, None), ("failed", "interrupted", "signaled", None),
            ("failed", "infra", "rate_limit", None), ("cancelled", "operator", None, "batch-abort"),
            ("cancelled", "operator", None, "unspecified")]]
        result = aggregate(events)
        self.assertAlmostEqual(result["success_rate"], 3 / 8)
        self.assertAlmostEqual(result["task_success_rate"], 3 / 4)
        self.assertEqual(result["outcome_classes"],
                         dict(success=3, task=1, infra=1, interrupted=1, operator=2))
        self.assertEqual(result["failure_reasons"], {"(none)": 1, "signaled": 1, "rate_limit": 1})
        self.assertEqual(result["cancel_reasons"], {"batch-abort": 1, "unspecified": 1})
        self.assertIsNone(aggregate([])["task_success_rate"])

    def test_cancel_reason_from_job(self):
        base = dict(id="x", status="cancelled")
        self.assertEqual(job_event(dict(base, cancel_reason="wrong-prompt"))["cancel_reason"], "wrong-prompt")
        self.assertEqual(job_event(dict(base, superseded_by="y"))["cancel_reason"], "superseded")
        self.assertEqual(job_event(base)["cancel_reason"], "unspecified")
        self.assertIsNone(job_event(dict(base, status="done"))["cancel_reason"])

    def test_old_event_borrows_failure_reason_from_done_record(self):
        old = dict(id="a", status="failed", exit=1, blocked_reason_class=None)
        self.assertEqual(classify_event(old)["outcome_class"], "task")
        self.assertEqual(classify_event(old, dict(failure_reason="rate_limit"))["outcome_class"], "infra")
        with tempfile.TemporaryDirectory() as tmp:
            root, now = Path(tmp), datetime(2026, 10, 17, 3, tzinfo=TZ)
            at = (now - timedelta(minutes=1)).isoformat()
            (root / "logs").mkdir()
            (root / "logs/events.jsonl").write_text(json.dumps(dict(old, at=at, project="p", pool="codex")) + "\n")
            (root / "done").mkdir()
            (root / "done/a.json").write_text(json.dumps(dict(id="a", status="failed", exit=1, finished_at=at,
                                                              failure_reason="upstream_transient")))
            result = collect_stats(root, now=now)
            self.assertEqual(result["sources"], dict(events=1, backfilled=0))
            self.assertEqual(result["outcome_classes"]["infra"], 1)
            self.assertEqual(result["failure_reasons"], {"upstream_transient": 1})


if __name__ == "__main__":
    unittest.main()
