"""Statistics stay read-only, tolerate old records, and prefer event observations."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
from stats import TZ, aggregate, blocked_class, collect_stats, format_stats, job_event, parse_since
from narrow import NARROW_CLASSES


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"
        self.now = datetime(2026, 10, 17, 3, tzinfo=TZ)

    def job(self, jid="one", **extra):
        job = dict(id=jid, status="done", project="cs", pool="codex", area="backend", tier="M",
                   started_at=(self.now - timedelta(minutes=2)).isoformat(),
                   finished_at=(self.now - timedelta(minutes=1)).isoformat())
        job.update(extra)
        return job

    def save(self, job):
        directory = self.root / "done"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (job["id"] + ".json")).write_text(json.dumps(job))

    def test_since_relative_iso_and_invalid(self):
        for text, minutes in [("2h", 120), ("30m", 30), ("1d", 1440)]:
            self.assertEqual(parse_since(text, self.now), self.now - timedelta(minutes=minutes))
        self.assertEqual(parse_since("2026-10-16T19:00:00Z"), self.now)
        self.assertEqual(parse_since("2026-10-17T03:00:00"), self.now)
        for text in ("", "-1h", "abc", "2w", "9" * 30 + "d"):
            with self.assertRaises(ValueError):
                parse_since(text, self.now)

    def test_empty_cli_creates_nothing_and_invalid_since_errors(self):
        env = dict(os.environ, CS_HOME=str(self.root), CS_TEST_FAKE_WORKER="1", PYTHONDONTWRITEBYTECODE="1")
        for args, code in [(["--json"], 0), (["--since", "bad", "--json"], 2), ([], 0)]:
            proc = subprocess.run(["/usr/bin/python3", str(REPO / "bin/cs"), "stats"] + args,
                                  env=env, text=True, capture_output=True)
            self.assertEqual(proc.returncode, code, proc.stderr)
            self.assertFalse(self.root.exists())
            if "--json" in args:
                result = json.loads(proc.stdout)
                self.assertEqual(result["schema_version"], "1")
                if not code:
                    self.assertIsNone(result["success_rate"])
                    self.assertEqual(result["elapsed_s"], dict(avg=None, p50=None, p95=None, samples=0))

    def test_backfill_filters_percentiles_dedup_and_event_preference(self):
        for i, status in enumerate(("done", "blocked", "failed", "unknown", "cancelled")):
            self.save(self.job(str(i), status=status, pool="cc" if i % 2 else "codex"))
        self.save(self.job("old", finished_at=(self.now - timedelta(hours=3)).isoformat()))
        self.save(self.job("future", finished_at=(self.now + timedelta(minutes=1)).isoformat()))
        self.save(self.job("legacy", finished_at=None))
        self.save(self.job("boundary", finished_at=(self.now - timedelta(hours=2)).isoformat(),
                           started_at=None, project="od", pool="opencode"))
        event = dict(job_event(self.job("0")), elapsed_s=100, prompt_bytes=1000)
        directory = self.root / "logs"
        directory.mkdir()
        (directory / "events.jsonl.1").write_text(json.dumps(event) + "\n")
        (directory / "events.jsonl").write_text(json.dumps(event) + '\nnot json\n[]\n{"id":')
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = collect_stats(self.root, now=self.now)
        self.assertEqual(result["total"], 6)
        self.assertAlmostEqual(result["success_rate"], 2 / 6)
        self.assertEqual(result["sources"], dict(events=1, backfilled=5))
        self.assertEqual(result["distribution"], dict(done=2, blocked=1, failed=1, unknown=1, cancelled=1))
        self.assertEqual(result["elapsed_s"], dict(avg=68, p50=60, p95=100, samples=5))
        self.assertEqual(result["mean_prompt_bytes"], 1000)
        self.assertEqual(result["by_pool"]["cc"]["total"], 2)
        self.assertEqual(result["by_project"]["od"]["total"], 1)
        self.assertEqual(collect_stats(self.root, pool="cc", project="cs", now=self.now)["total"], 2)
        self.assertEqual(collect_stats(self.root, since="30m", now=self.now)["total"], 5)
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_heuristics_prompt_size_and_exact_done(self):
        self.assertEqual(blocked_class(self.job(status="blocked", need_files=["extra.py"]),
                                       "read-only cannot write output"), "files_narrow")
        self.assertIsNone(blocked_class(self.job(status="done", need_files=["extra.py"]), ""))
        conclusion = Path(self.temp.name) / "conclusion.md"
        prompt = Path(self.temp.name) / "prompt.md"
        prompt.write_text("中文 prompt")
        for text, kind in [("清单外文件 tests/foo.py", "files_narrow"),
                           ("read-only cannot write output", "read_only_write"),
                           ("主稿路径不存在", "bad_input"), ("PR 已开 CI pending", "soft_block"),
                           ("等待人类决定", "other")]:
            self.assertEqual(blocked_class(self.job(status="blocked"), text), kind)
        for text, expected in [(" STATUS: DONE \n", True), ("body STATUS: DONE", False),
                               ("STATUS: DONE later", False), ("status: done", False)]:
            conclusion.write_text(text)
            event = job_event(self.job(status="cancelled", conclusion=str(conclusion), prompt_file=str(prompt),
                                      queue_reasons=[{"type": "lock"}, {"type": "lock"}, {"type": "pool_full"}]))
            self.assertEqual(event["cancelled_after_done"], expected)
            self.assertIsNone(event["blocked_reason_class"])
            self.assertEqual(event["prompt_bytes"], len(prompt.read_bytes()))
            self.assertEqual(event["queue_reason_types"], ["lock", "pool_full"])
        prompt.unlink()
        self.assertIsNone(job_event(self.job(prompt_file=str(prompt)))["prompt_bytes"])

    def test_jsonl_rotation_keeps_complete_lines_and_retention(self):
        with patch.dict(os.environ, {"CS_EVENT_LOG_MAX_BYTES": "1000", "CS_EVENT_LOG_BACKUPS": "2"}):
            for i in range(12):
                core.log_event(self.root, self.job(str(i)))
        files = sorted((self.root / "logs").glob("events.jsonl*"))
        self.assertEqual(len(files), 3)
        self.assertEqual(json.loads(files[0].read_text().splitlines()[-1])["id"], "11")
        for path in files:
            self.assertLessEqual(path.stat().st_size, 1000)
            for line in path.read_text().splitlines():
                self.assertIn(json.loads(line)["status"], ("done",))
        with patch.dict(os.environ, {"CS_EVENT_LOG_MAX_BYTES": "256", "CS_EVENT_LOG_BACKUPS": "0"}):
            core.log_event(self.root, self.job("last"))
        self.assertEqual(len(list((self.root / "logs").glob("events.jsonl*"))), 1)

    def test_expanded_blocked_classes_and_ordering(self):
        cases = {
            "files_narrow": ["需追加授权", "需额外授权", "需新增授权", "未获写授权",
                             "缺少必要文件授权", "不在白名单", "outside the allowed scope",
                             "outside the writable scope", "NEED_FILES: lib/extra.py"],
            "pre_existing_main_failure": ["also reproduced on origin/main", "main 上同样失败",
                                         "pre-existing failure", "已在 main 失败"],
            "upstream_transient": ["Transport channel closed", "API Error: 503",
                                   "model is at capacity", "connection reset"],
        }
        events = []
        for kind, texts in cases.items():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(blocked_class(self.job(status="blocked"), text), kind)
                    self.assertIsNone(blocked_class(self.job(status="failed"), text))
                events.append(dict(status="blocked", blocked_reason_class=kind))
        classes = aggregate(events)["blocked_reason_classes"]
        self.assertEqual(list(classes), ["files_narrow", "read_only_write", "pre_existing_main_failure",
                                         "upstream_transient", "soft_block", "bad_input", "other"])
        for kind, texts in cases.items():
            self.assertEqual(classes[kind], len(texts))

    def test_event_uses_last_queue_observation(self):
        reasons = [{"type": "lock"}, {"type": "pool_full"}, {"type": "lock"}]
        event = job_event(self.job(last_queue_reasons=reasons, queue_reasons=[], queue_wait_s=12.5))
        self.assertEqual(event["queue_reason_types"], ["lock", "pool_full"])
        self.assertEqual(event["last_queue_reasons"], reasons)
        self.assertEqual(event["queue_wait_s"], 12.5)

    def test_narrow_subcauses_preserve_parent_class_and_aggregate(self):
        events = []
        base = dict(status="blocked", files=["src/x.py"], need_files=["tests/test_x.py"],
                    scope_policy="companion")
        cases = [("companion_miss", {}), ("strict_companion_known", {"scope_policy": "strict"}),
                 ("shared_or_core", {"protected_paths": ["tests"]}),
                 ("permission_or_lock", {"read_only": True}),
                 ("unclear_input", {"need_files": ["src/x.py"]}),
                 ("other", {"scope_policy": "strict", "need_files": ["src/business.py"]})]
        for kind, extra in cases:
            event = job_event(self.job(kind, **dict(base, **extra)))
            self.assertEqual(event["blocked_reason_class"], "files_narrow")
            self.assertEqual(event["narrow_reason_class"], kind)
            self.assertEqual(event["outcome_class"], "task")
            events.append(event)
        grouped = aggregate(events)
        self.assertEqual(grouped["narrow_reason_classes"], dict.fromkeys(NARROW_CLASSES, 1))
        self.assertEqual(grouped["blocked_reason_classes"]["files_narrow"], 6)
        self.assertIsNone(job_event(self.job(need_files=["tests/test_x.py"]))["narrow_reason_class"])

    def test_old_events_enrich_subcause_read_only_even_with_outcome_class(self):
        job = self.job(status="blocked", files=["src/x.py"], need_files=["tests/test_x.py"],
                       scope_policy="strict")
        self.save(job)
        event = job_event(job)
        event.pop("narrow_reason_class")
        logs = self.root / "logs"
        logs.mkdir()
        path = logs / "events.jsonl"
        path.write_text(json.dumps(event) + "\n")
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = collect_stats(self.root, now=self.now)
        self.assertEqual(result["narrow_reason_classes"]["strict_companion_known"], 1)
        self.assertEqual(result["by_pool"]["codex"]["narrow_reason_classes"]["strict_companion_known"], 1)
        self.assertIn("strict_companion_known=1", format_stats(result))
        self.assertEqual(before, {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        # Rotated events without a done record have insufficient attribution evidence.
        (self.root / "done/one.json").unlink()
        result = collect_stats(self.root, now=self.now)
        self.assertEqual(result["narrow_reason_classes"]["unclear_input"], 1)

    def test_new_event_subcause_is_not_reclassified_from_a_changed_conclusion(self):
        job = self.job(status="blocked", files=["src/x.py"], need_files=["tests/test_x.py"],
                       scope_policy="strict")
        self.save(dict(job, scope_policy="companion"))
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "events.jsonl").write_text(json.dumps(job_event(job)) + "\n")
        result = collect_stats(self.root, now=self.now)
        self.assertEqual(result["narrow_reason_classes"]["strict_companion_known"], 1)
        self.assertEqual(result["narrow_reason_classes"]["companion_miss"], 0)
        # Backfill also preserves the attribution captured at completion.
        saved = dict(job, scope_policy="companion", narrow_reason_class="strict_companion_known")
        self.assertEqual(job_event(saved)["narrow_reason_class"], "strict_companion_known")


if __name__ == "__main__":
    unittest.main()
