"""Read-only cleanup candidates; sizes are best-effort and share a time budget."""
import os
from pathlib import Path
import stat
import subprocess
import time
from datetime import datetime, timedelta

import core
from stats import timestamp


def _git(cwd, *args, return_code=False):
    try:
        result = subprocess.run(["git", "--no-optional-locks", "-C", str(cwd), *args],
                                capture_output=True, text=True, timeout=2)
        if return_code:
            return result.returncode if result.returncode in (0, 1) else None
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _size(paths, deadline):
    total, partial, pending = 0, False, list(paths)
    while pending:
        if time.monotonic() >= deadline:
            return total, True
        path = pending.pop()
        try:
            info = os.stat(path, follow_symlinks=False)
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
            elif stat.S_ISDIR(info.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline:
                            return total, True
                        if not entry.is_symlink():
                            pending.append(entry.path)
        except OSError:
            partial = True
    return total, partial


def cleanup_report(root, days=7, budget_s=10.0, now=None) -> dict:
    """List candidates only; no state initialization, locks, or filesystem writes."""
    root = Path(root)
    cutoff = ((now or datetime.now(core.TZ)) - timedelta(days=days)).timestamp()
    active = core.load_jobs(root, "queue") + core.load_jobs(root, "running")
    done = core.load_jobs(root, "done")
    active_ids = {job.get("id") for job in active}
    records = {job.get("id"): job for job in done}
    active_cwds = {str(Path(job["cwd"]).resolve()) for job in active if job.get("cwd")}
    report = {name: {"count": 0, "items": [], "bytes": 0}
              for name in ("job_dirs", "worktrees", "state_files")}

    def ended(job):
        value = timestamp(job.get("finished_at"))
        return value.timestamp() if value else float("inf")

    workers = core.remote_dir("__cleanup_probe__").parent
    if workers.is_dir():
        with os.scandir(workers) as entries:
            for entry in entries:
                try:
                    if entry.name in active_ids or not entry.is_dir(follow_symlinks=False):
                        continue
                    old = (ended(records[entry.name]) if entry.name in records
                           else entry.stat(follow_symlinks=False).st_mtime)
                    if old < cutoff:
                        report["job_dirs"]["items"].append(entry.path)
                except OSError:
                    continue
    latest = {}
    for job in done:
        if job.get("cwd"):
            cwd = str(Path(job["cwd"]).resolve())
            latest[cwd] = max(latest.get(cwd, float("-inf")), ended(job))
    details = {}
    for cwd, finished in sorted(latest.items()):
        if cwd in active_cwds or finished >= cutoff:
            continue
        git_dir, common = _git(cwd, "rev-parse", "--git-dir"), _git(cwd, "rev-parse", "--git-common-dir")
        if git_dir is None or common is None or (Path(cwd) / git_dir).resolve() == (Path(cwd) / common).resolve():
            continue
        branch = _git(cwd, "symbolic-ref", "--short", "HEAD")
        merged = None
        if _git(cwd, "rev-parse", "--verify", "refs/heads/main") is not None:
            code = _git(cwd, "merge-base", "--is-ancestor", "HEAD", "refs/heads/main", return_code=True)
            merged = None if code is None else code == 0
        report["worktrees"]["items"].append(cwd)
        details[cwd] = {"branch": branch, "merged": merged}
    report["worktrees"]["details"] = details
    if root.is_dir():
        with os.scandir(root) as entries:
            report["state_files"]["items"] = [e.path for e in entries if e.is_file(follow_symlinks=False)
                and (e.name.startswith("须续跑-") or ".bak" in e.name or e.name.endswith((".tgz", ".tar.gz")))]
    deadline = time.monotonic() + max(0.0, budget_s)
    for category in report.values():
        category["items"].sort()
        category["count"] = len(category["items"])
        category["bytes"], partial = _size(category["items"], deadline)
        if partial:
            category["partial"] = True
    return report
