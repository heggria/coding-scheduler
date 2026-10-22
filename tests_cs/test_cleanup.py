"""Isolated cleanup report and CLI checks; no model workers."""
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
from cleanup import cleanup_report


class CleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / "state"
        env = patch.dict(os.environ, {"CS_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        core.ensure()
        self.now = datetime.now(core.TZ)
        self.old = self.now - timedelta(days=9)
        self.repo, self.wt = self.base / "repo", self.base / "linked"
        self.git("init", "-b", "main", str(self.repo))
        self.git("-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-m", "initial")
        self.git("-C", str(self.repo), "worktree", "add", "-b", "old", str(self.wt))
        for jid, folder in (("old", "done"), ("new", "done"), ("live", "running"), ("queued", "queue")):
            core.write_job(self.root, folder, dict(id=jid, cwd=str(self.wt) if jid == "old" else str(self.repo),
                status=folder, finished_at=(self.now if jid == "new" else self.old).isoformat()))
            core.remote_dir(jid).mkdir(parents=True)
        core.remote_dir("orphan").mkdir()
        for path in (self.root / "workers").iterdir():
            os.utime(path, (self.old.timestamp(), self.old.timestamp()))
        core.remote_dir("recent-orphan").mkdir()
        (core.remote_dir("orphan") / "link").symlink_to(self.repo, target_is_directory=True)
        os.utime(core.remote_dir("orphan"), (self.old.timestamp(), self.old.timestamp()))
        for name in ("须续跑-x.md", "a.bak-1", "b.tgz", "c.tar.gz", "ordinary"):
            (self.root / name).write_text("123")

    def git(self, *args):
        return subprocess.run(["git", *args], check=True, capture_output=True, timeout=10)

    def test_report_cli_and_no_deletion(self):
        before = {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()}
        report = cleanup_report(self.root, now=self.now)
        self.assertEqual(report["job_dirs"]["items"], sorted(str(core.remote_dir(j)) for j in ("old", "orphan")))
        self.assertEqual(report["state_files"]["count"], 4)
        self.assertEqual({Path(p).name for p in report["state_files"]["items"]},
                         {"须续跑-x.md", "a.bak-1", "b.tgz", "c.tar.gz"})
        self.assertEqual(report["job_dirs"]["bytes"], 0)
        self.assertNotIn(str(self.root / "ordinary"), report["state_files"]["items"])
        self.assertEqual(report["state_files"]["bytes"], 12)
        self.assertEqual(report["worktrees"]["items"], [str(self.wt)])
        self.assertEqual(report["worktrees"]["details"][str(self.wt)], dict(branch="old", merged=True))
        self.assertTrue(cleanup_report(self.root, budget_s=0)["job_dirs"]["partial"])
        for args in (("cleanup", "--json"), ("--json", "cleanup"), ("cleanup", "--days", "7")):
            out = subprocess.check_output([sys.executable, str(REPO / "bin/cs"), *args], text=True, timeout=20)
            self.assertIn("job_dirs", json.loads(out) if "--json" in args else out)
            if "--json" not in args:
                self.assertIn("仅列出，未删除", out)
        self.assertEqual(before, {p: p.read_bytes() for p in self.base.rglob("*") if p.is_file()})
        self.git("-C", str(self.wt), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                 "commit", "--allow-empty", "-m", "unmerged")
        self.assertFalse(cleanup_report(self.root)["worktrees"]["details"][str(self.wt)]["merged"])
        self.git("-C", str(self.repo), "branch", "-m", "main", "other")
        self.assertIsNone(cleanup_report(self.root)["worktrees"]["details"][str(self.wt)]["merged"])
        core.write_job(self.root, "queue", dict(id="busy", cwd=str(self.wt)))
        self.assertEqual(cleanup_report(self.root)["worktrees"]["items"], [])
        core.write_job(self.root, "queue", dict(id="busy", cwd=str(self.repo)))
        core.write_job(self.root, "done", dict(id="recent", cwd=str(self.wt), finished_at=self.now.isoformat()))
        self.assertEqual(cleanup_report(self.root)["worktrees"]["items"], [])
