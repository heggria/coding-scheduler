"""Repo/worktree-aware lock resolution; no scheduler state access."""
from __future__ import annotations

import subprocess
from pathlib import Path


def nearest_directory(path):
    path = Path(path)
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


class GitLockResolver:
    """Callable directory -> (common git dir, worktree root), cached per pass.

    A missing/broken Git installation, non-repo directory or timeout returns
    None. Tests can inject a callable with the same contract instead.
    """

    def __init__(self, run=subprocess.run, timeout=2):
        self.run = run
        self.timeout = timeout
        self.cache = {}

    def __call__(self, directory):
        directory = nearest_directory(Path(directory).resolve())
        if directory not in self.cache:
            self.cache[directory] = self._lookup(directory)
        return self.cache[directory]

    def _lookup(self, directory):
        def rev_parse(*args):
            result = self.run(
                ["git", "-C", str(directory), "rev-parse", *args],
                check=True, capture_output=True, text=True, timeout=self.timeout,
            )
            return result.stdout.strip()

        try:
            common = rev_parse("--path-format=absolute", "--git-common-dir")
            # Older rev-parse versions echo unknown flags instead of failing.
            if common.startswith("--path-format="):
                common = rev_parse("--git-common-dir")
            root = rev_parse("--show-toplevel")
            if not common or not root:
                return None
            return (
                str((directory / common).resolve()),
                str((directory / root).resolve()),
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            return None


def lock_context(job, resolver=None):
    """Normalize lock entries, reusing a submit snapshot when inputs match.

    resolver(directory) returns (repo_id, worktree_root) or None. Non-git
    entries use an absolute relpath and None for both scope fields.
    """
    resolver = resolver if resolver is not None else GitLockResolver()
    cwd = Path(job.get("cwd") or Path.home()).expanduser().resolve()
    inputs = {
        "version": 1,
        "cwd": str(cwd),
        "files": list(job.get("files") or []),
        "shared_files": list(job.get("shared_files") or []),
    }
    stored = job.get("lock_ctx")
    if (isinstance(stored, dict) and isinstance(stored.get("locks"), list)
            and all(stored.get(k) == v for k, v in inputs.items())):
        return stored

    locks = []
    if inputs["files"] or inputs["shared_files"]:
        cwd_repo = resolver(cwd)
        for field, kind in (("files", "write"), ("shared_files", "shared")):
            for raw in inputs[field]:
                path = (cwd / Path(raw).expanduser()).resolve()
                repo = cwd_repo
                if repo is None or not path.is_relative_to(repo[1]):
                    repo = resolver(nearest_directory(path))
                if repo is not None and path.is_relative_to(repo[1]):
                    repo_id, root = repo
                    relpath = path.relative_to(root).as_posix()
                else:
                    repo_id, root, relpath = None, None, str(path)
                locks.append({
                    "path": raw, "kind": kind, "repo_id": repo_id,
                    "worktree_root": root, "relpath": relpath,
                })
    return dict(inputs, locks=locks)


def directory_locks(context):
    """Yield original lock paths whose broad prefix should warn at submit."""
    cwd = Path(context["cwd"])
    seen = set()
    for lock in context["locks"]:
        raw = lock["path"]
        path = (cwd / Path(raw).expanduser()).resolve()
        if path not in seen and (raw.endswith("/") or path.is_dir() or path == cwd
                or str(path) == lock["worktree_root"]):
            seen.add(path)
            yield raw


def path_overlap(a, b):
    # '.' represents the entire Git worktree. '/' is the non-git root.
    if a in (".", "/") or b in (".", "/"):
        return True
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def file_conflict(job, running, resolver=None):
    """Return structured conflicts; an empty list preserves old truthiness."""
    resolver = resolver if resolver is not None else GitLockResolver()
    mine = lock_context(job, resolver)["locks"]
    conflicts = []
    seen = set()
    if not mine:
        return conflicts
    for other in running:
        theirs = lock_context(other, resolver)["locks"]
        for m in mine:
            for t in theirs:
                if m["repo_id"] != t["repo_id"]:
                    continue
                shared = "shared" in (m["kind"], t["kind"])
                if not shared and m["worktree_root"] != t["worktree_root"]:
                    continue
                if path_overlap(m["relpath"], t["relpath"]):
                    kind = "shared" if shared else "write"
                    key = (other["id"], m["repo_id"], m["worktree_root"], m["relpath"], kind)
                    if key in seen:
                        continue
                    seen.add(key)
                    conflicts.append({
                        "job": other["id"], "mine": m["path"],
                        "theirs": t["path"],
                        "kind": kind,
                        "repo": m["repo_id"],
                    })
    return conflicts
