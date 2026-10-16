"""All scheduler state and Git fixtures live in temporary directories."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "lib"))
import core
from locks import GitLockResolver, file_conflict, lock_context


def job(jid, cwd, files=(), shared=()):
    return {"id": jid, "cwd": str(cwd), "files": list(files),
            "shared_files": list(shared)}


class LockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.main = self.base / "main"
        self.wt = self.base / "worktree"
        self.other = self.base / "other"
        self.common = str(self.main / ".git")

        def resolve(directory):
            path = Path(directory)
            for root in (self.main, self.wt, self.other):
                if path.is_relative_to(root):
                    return (str(root / ".git") if root == self.other else self.common,
                            str(root))
            return None

        self.resolver = Mock(side_effect=resolve)

    def conflict(self, a, b):
        return file_conflict(a, [b], self.resolver)

    def test_r130_worktrees_parallel_by_default(self):
        self.assertFalse(self.conflict(
            job("alert", self.main, ["src/gateway_console/agent.py"]),
            job("sweep", self.wt, ["src/gateway_console/agent.py"])))

    def test_shared_conflicts_with_write_in_either_direction(self):
        a = job("alert", self.main, shared=["src/gateway_console/agent.py"])
        b = job("sweep", self.wt, ["src/gateway_console/agent.py"])
        for mine, other in ((a, b), (b, a)):
            with self.subTest(mine=mine["id"]):
                self.assertEqual(self.conflict(mine, other), [{
                    "job": other["id"], "mine": "src/gateway_console/agent.py",
                    "theirs": "src/gateway_console/agent.py",
                    "kind": "shared", "repo": self.common}])

    def test_shared_conflicts_with_shared(self):
        self.assertTrue(self.conflict(job("a", self.main, shared=["src/"]),
                                     job("b", self.wt, shared=["src/a.py"])))

    def test_same_worktree_file_conflicts(self):
        conflicts = self.conflict(job("a", self.main, ["src/a.py"]),
                                  job("b", self.main, ["src/a.py"]))
        self.assertEqual(conflicts[0]["kind"], "write")

    def test_directory_overlap_and_component_boundary(self):
        for a, b, expected in (("src/", "src/a.py", True),
                               ("src/a.py", "src", True),
                               ("src", "src-other/a.py", False),
                               (".", "elsewhere/a.py", True)):
            with self.subTest(a=a, b=b):
                self.assertEqual(bool(self.conflict(job("a", self.main, [a]),
                                                   job("b", self.main, [b]))), expected)

    def test_different_repos_do_not_conflict_even_when_shared(self):
        self.assertFalse(self.conflict(job("a", self.main, shared=["src/a.py"]),
                                       job("b", self.other, ["src/a.py"])))

    def test_non_git_absolute_paths_and_prefix_boundary(self):
        folder = str(self.base / "data")
        a = job("a", self.main, [folder + "/"])
        self.assertEqual(self.conflict(a, job("b", self.wt, [folder + "/new/file"]))[0],
                         {"job": "b", "mine": folder + "/",
                          "theirs": folder + "/new/file", "kind": "write", "repo": None})
        self.assertFalse(self.conflict(a, job("b", self.wt, [folder + "2/file"])))
        self.assertTrue(self.conflict(job("a", self.base, ["/"]),
                                      job("b", self.base, [folder])))

    def test_non_git_relative_paths_are_cwd_relative(self):
        self.assertTrue(self.conflict(job("a", self.base, ["data"]),
                                      job("b", self.wt, [str(self.base / "data/file")])))
        self.assertFalse(self.conflict(job("a", self.base / "a", ["src/file"]),
                                       job("b", self.base / "b", ["src/file"])))

    def test_old_job_without_context_or_shared_files(self):
        a = {"id": "old", "cwd": str(self.main), "files": ["src/a.py"]}
        self.assertTrue(self.conflict(a, job("b", self.main, ["src/a.py"])))
        self.assertNotIn("lock_ctx", a)
        self.assertFalse(self.conflict(a, job("b", self.wt, ["src/a.py"])))

    def test_absolute_path_inside_worktree_and_subdirectory_cwd(self):
        a = job("a", self.main / "src", [str(self.main / "src/a.py"), "./b/../a.py"])
        ctx = lock_context(a, self.resolver)
        self.assertEqual([k["relpath"] for k in ctx["locks"]], ["src/a.py"] * 2)
        self.assertEqual(ctx["locks"][0]["worktree_root"], str(self.main))
        self.assertTrue(self.conflict(a, job("b", self.main, ["src/a.py"])))

    def test_outside_absolute_path_uses_its_own_repo(self):
        a = job("a", self.main, [str(self.other / "src/future/file.py")])
        # The own-repo lookup uses the nearest existing directory.
        self.other.mkdir()
        ctx = lock_context(a, self.resolver)
        self.assertEqual(ctx["locks"][0]["repo_id"], str(self.other / ".git"))
        self.assertEqual(ctx["locks"][0]["relpath"], "src/future/file.py")
        self.assertTrue(self.conflict(a, job("b", self.other, ["src/future/file.py"])))

    def test_submit_snapshot_reused_and_changed_inputs_recomputed(self):
        a = job("a", self.main, ["src/a.py"])
        # Store a JSON snapshot, as submit does.
        a["lock_ctx"] = json.loads(json.dumps(lock_context(a, self.resolver)))
        self.resolver.reset_mock()
        self.assertEqual(lock_context(a, self.resolver), a["lock_ctx"])
        self.resolver.assert_not_called()
        a["shared_files"] = ["src/b.py"]
        self.assertEqual(len(lock_context(a, self.resolver)["locks"]), 2)
        self.resolver.assert_called()

    def test_context_snapshot_does_not_alias_mutable_inputs(self):
        a = job("a", self.main, ["src/a.py"])
        a["lock_ctx"] = lock_context(a, self.resolver)
        a["files"].append("src/b.py")
        self.assertEqual(len(lock_context(a, self.resolver)["locks"]), 2)

    def test_all_conflicts_reported_and_empty_files_are_falsy(self):
        a = job("a", self.main, ["src"])
        others = [job("b", self.main, ["src/a.py"]), job("c", self.main, ["src/b.py"])]
        self.assertEqual([c["job"] for c in file_conflict(a, others, self.resolver)], ["b", "c"])
        self.assertFalse(file_conflict(job("empty", self.main), others, self.resolver))


class ResolverTests(unittest.TestCase):
    def test_git_failures_are_cached_non_git_fallback(self):
        for error in (FileNotFoundError("git"), subprocess.TimeoutExpired("git", 2),
                      subprocess.CalledProcessError(128, "git")):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as tmp:
                run = Mock(side_effect=error)
                resolve = GitLockResolver(run=run)
                self.assertIsNone(resolve(tmp))
                self.assertIsNone(resolve(tmp))
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs["timeout"], 2)
                ctx = lock_context(job("a", tmp, ["future/file"]), resolve)
                self.assertIsNone(ctx["locks"][0]["repo_id"])
                self.assertEqual(ctx["locks"][0]["relpath"], str(Path(tmp).resolve() / "future/file"))

    def test_old_git_relative_common_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            run = Mock(side_effect=[Mock(stdout="--path-format=absolute\n../.git\n"),
                                    Mock(stdout="../.git\n"), Mock(stdout=str(root) + "\n")])
            resolve = GitLockResolver(run=run)
            self.assertEqual(resolve(root), (str(root.parent / ".git"), str(root)))


class GitIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.base = Path(cls.tmp.name).resolve()
        cls.main = cls.base / "repo"
        cls.wt = cls.base / "linked"

        def git(*args):
            return subprocess.run(["git", *map(str, args)], check=True,
                                  capture_output=True, text=True, timeout=10)

        git("init", cls.main)
        git("-C", cls.main, "-c", "user.name=Lock Test", "-c", "user.email=lock@example.invalid",
            "commit", "--allow-empty", "-m", "fixture")
        git("-C", cls.main, "worktree", "add", "-b", "test-linked", cls.wt)

    def test_real_git_worktree_r130_and_absolute_normalization(self):
        resolve = GitLockResolver()
        common, main_root = resolve(self.main)
        self.assertEqual(common, str(self.main / ".git"))
        self.assertEqual(main_root, str(self.main))
        self.assertEqual(resolve(self.wt), (common, str(self.wt)))
        a = job("alert", self.main, ["src/gateway_console/agent.py"])
        b = job("sweep", self.wt, [str(self.wt / "src/gateway_console/agent.py")])
        self.assertFalse(file_conflict(a, [b], resolve))
        a["shared_files"] = a.pop("files")
        self.assertEqual(file_conflict(a, [b], resolve)[0]["kind"], "shared")
        self.assertEqual(lock_context(b, resolve)["locks"][0]["relpath"], "src/gateway_console/agent.py")

    def test_external_absolute_path_nearest_parent_and_git_cache(self):
        run = Mock(wraps=subprocess.run)
        resolve = GitLockResolver(run=run)
        a = job("external", self.main, [str(self.wt / "future/deep/file.py")])
        ctx = lock_context(a, resolve)
        self.assertEqual(ctx["locks"][0]["repo_id"], str(self.main / ".git"))
        self.assertEqual(ctx["locks"][0]["worktree_root"], str(self.wt))
        self.assertEqual(ctx["locks"][0]["relpath"], "future/deep/file.py")
        calls = run.call_count
        self.assertTrue(file_conflict(a, [job("b", self.wt, ["future/deep/file.py"])], resolve))
        self.assertEqual(run.call_count, calls)
        self.assertFalse(file_conflict(a, [job("c", self.main, ["future/deep/file.py"])], resolve))

    def test_non_git_symlink_resolves_to_same_absolute_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            data = root / "data"
            data.mkdir()
            (root / "alias").symlink_to(data, target_is_directory=True)
            a = job("a", self.main, [str(root / "alias")])
            b = job("b", self.wt, [str(data / "future/file")])
            conflicts = file_conflict(a, [b])
            self.assertEqual(conflicts[0]["repo"], None)

    def test_tick_isolated_state_no_launch_and_blocked_by_changes_only(self):
        with tempfile.TemporaryDirectory() as state, patch.dict(os.environ, {"CODING_SCHEDULER_HOME": state}):
            root = core.ensure()
            active = job("active", self.main, ["src/agent.py"])
            active.update(pool="codex", pid=os.getpid(), status="running")
            parallel = job("parallel", self.wt, ["src/agent.py"])
            blocked = job("shared", self.wt, shared=["src/agent.py"])
            same = job("same", self.main, ["src/agent.py"])
            # All jobs intentionally have the old format, with no lock_ctx.
            core.write_job(root, "running", active)
            for queued in (parallel, blocked, same):
                core.write_job(root, "queue", queued)

            def fake_launch(root, queued, pool):
                queued.update(pool=pool, pid=os.getpid(), status="running")
                core.remove_job(root, "queue", queued["id"])
                core.write_job(root, "running", queued)

            with patch.object(core, "launch", side_effect=fake_launch) as launch, \
                    patch.object(core, "remote_dir", side_effect=lambda jid: root / "workers" / jid):
                self.assertEqual(core.tick(root)["started"], ["parallel"])
                launch.assert_called_once()
                queued = {j["id"]: j for j in core.load_jobs(root, "queue")}
                self.assertEqual(queued["same"]["blocked_by"][0]["kind"], "write")
                self.assertEqual(queued["shared"]["blocked_by"][0]["kind"], "shared")
                # A second pass records the additional newly launched blocker.
                self.assertFalse(core.tick(root)["started"])
                with patch.object(core, "write_job", wraps=core.write_job) as write:
                    self.assertFalse(core.tick(root)["started"])
                    write.assert_not_called()
                for running in core.load_jobs(root, "running"):
                    core.remove_job(root, "running", running["id"])
                # Unblock one job; fake launch observes the cleared metadata.
                core.remove_job(root, "queue", "shared")
                self.assertEqual(core.tick(root)["started"], ["same"])
                self.assertNotIn("blocked_by", core.load_jobs(root, "running")[0])

    def submit(self, root, jid, files="", shared=""):
        prompt = root / "prompt.md"
        prompt.write_text("fixture; do not launch\n")
        env = dict(os.environ, CODING_SCHEDULER_HOME=str(root / "state"))
        return subprocess.run([
            sys.executable, str(REPO / "bin/cs"), "submit", "--project", "fixture",
            "--area", "backend", "--tier", "M", "--cwd", str(self.wt),
            "--prompt", str(prompt), "--id", jid, "--files", files,
            "--lock-shared", shared,
        ], env=env, capture_output=True, text=True, timeout=15)

    def test_submit_warns_directory_cwd_root_and_trailing_slash(self):
        (self.wt / "existing-dir").mkdir(exist_ok=True)
        (self.wt / "subdir").mkdir(exist_ok=True)
        for path in ("existing-dir", ".", str(self.wt), "future-dir/"):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                result = self.submit(root, "dir", files=path)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("WARNING: directory lock", result.stderr)
                saved = json.loads((root / "state/queue/dir.json").read_text())
                self.assertEqual(saved["files"], [path])
                self.assertIn("lock_ctx", saved)

    def test_submit_shared_files_context_and_directory_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.submit(root, "shared", files="src/a.py", shared="src/b.py, future/")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("WARNING: directory lock 'future/'", result.stderr)
            saved = json.loads((root / "state/queue/shared.json").read_text())
            self.assertEqual(saved["shared_files"], ["src/b.py", "future/"])
            self.assertEqual([k["kind"] for k in saved["lock_ctx"]["locks"]], ["write", "shared", "shared"])

    def test_submit_file_has_no_directory_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.submit(Path(tmp), "file", files="src/a.py")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
