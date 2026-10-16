"""Queue / launch / board helpers for cs CLI."""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shlex
import shutil
import signal
import sys
import time
from contextlib import contextmanager
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from affinity import CC_CAP, CODEX_CAP, model_for, pick_pool, score
from locks import GitLockResolver, file_conflict

TZ = timezone(timedelta(hours=8))
NO_SUB = (
    "HARD RULE: do not spawn sub-agents, nested agents, or background agent workers. "
    "Stay single-threaded in this session.\n\n"
)


def home():
    return Path(os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME")
                or Path.home() / ".coding-scheduler").expanduser().resolve()


def ensure(root=None):
    root = root or home()
    for n in ("queue", "running", "done", "projects"):
        (root / n).mkdir(parents=True, exist_ok=True)
    return root


def load_jobs(root, folder):
    out = []
    for p in sorted((root / folder).glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except Exception:
            pass
    return out


def write_job(root, folder, job):
    prompt = root / "queue" / (job["id"] + ".prompt.md")
    archive_prompt = (folder == "done" and
                      job.get("status") in ("done", "failed", "blocked", "cancelled") and
                      prompt.is_file())
    if archive_prompt:
        destination = remote_dir(job["id"]) / "prompt.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prompt, destination)
        job["prompt_file"] = str(destination)
    path = root / folder / ("%s.json" % job["id"])
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)
    if archive_prompt:
        prompt.unlink()


def remove_job(root, folder, jid):
    p = root / folder / ("%s.json" % jid)
    if p.exists():
        p.unlink()


def counts(running):
    c = {"codex": 0, "cc": 0}
    for j in running:
        if j.get("pool") in c:
            c[j["pool"]] += 1
    return c


def area_counts(running):
    d = {}
    for j in running:
        k = (j.get("pool"), j.get("area"))
        d[k] = d.get(k, 0) + 1
    return d


@contextmanager
def state_lock(root):
    """Serialize CLI state transitions (including idempotent submission)."""
    with (root / ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def remote_dir(jid):
    # An isolated scheduler must never reuse production worker artifacts.
    if os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME"):
        return home() / "workers" / jid
    return Path.home() / ".remote-jobs" / jid


def find_job(root, jid):
    for folder in ("queue", "running", "done"):
        path = root / folder / (jid + ".json")
        if path.exists():
            return json.loads(path.read_text())
    return None


def git_output(cwd, *args):
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True,
                              text=True, timeout=10)
        return proc.stdout.rstrip("\n") if proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def process_group_members(pgid):
    """Return (pid, state) pairs, including zombies, from a fresh snapshot."""
    try:
        result = subprocess.run(["ps", "-axo", "pid=,pgid=,stat="],
                                capture_output=True, text=True, check=True, timeout=5)
    except subprocess.SubprocessError as exc:
        raise OSError("cannot inspect process group %s: %s" % (pgid, exc)) from exc
    members = []
    for line in result.stdout.splitlines():
        pid, group, state = line.split()
        if int(group) == pgid:
            members.append((int(pid), state))
    return members


def kill_worker_group(pgid):
    """Terminate a worker session; return evidence only after no live members remain."""
    pgid = int(pgid)
    if pgid <= 1 or pgid == os.getpgrp():
        raise ValueError("refusing unsafe worker process group: %s" % pgid)
    grace = float(os.environ.get("CS_CANCEL_GRACE", "5"))
    if not math.isfinite(grace) or grace < 0:
        raise ValueError("CS_CANCEL_GRACE must be a finite nonnegative number")
    evidence = {"cancel_killed_pids": [pid for pid, _ in process_group_members(pgid)],
                "cancel_signal": None}

    def alive():
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # macOS returns EPERM for a group that holds only zombies.
            pass
        # Unreaped zombies cannot execute or receive signals; do not wait on them.
        return any(not state.startswith("Z") for _, state in process_group_members(pgid))

    def send(sig):
        try:
            os.killpg(pgid, sig)
            evidence["cancel_signal"] = sig.name
        except ProcessLookupError:
            pass
        except PermissionError:
            if alive():
                raise

    def wait_gone(seconds):
        deadline = time.monotonic() + seconds
        while alive():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.05, remaining))
        return True

    if alive():
        send(signal.SIGTERM)
        if not wait_gone(grace):
            send(signal.SIGKILL)
            if not wait_gone(5):
                raise OSError("worker process group %s remains alive after SIGKILL" % pgid)
    return evidence


def job_summary(job, lines=20):
    """Bounded bot output; old records need no migration."""
    started = job.get("started_at")
    elapsed = None
    if started:
        try:
            begin = datetime.fromisoformat(started)
            end = datetime.fromisoformat(job["finished_at"]) if job.get("finished_at") else datetime.now(TZ)
            elapsed = round(max(0, (end - begin).total_seconds()), 3)
        except (ValueError, TypeError):
            pass
    jd = remote_dir(job["id"])
    conclusion = job.get("conclusion")
    text = ""
    if conclusion:
        try:
            text = Path(conclusion).read_text(errors="replace")
        except OSError:
            pass
    links = re.findall(
        r"(?<![A-Za-z0-9_.-])(?:https?://)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[0-9]+\b", text)
    result = {"id": job["id"], "status": job.get("status", "queued"),
              "exit": job.get("exit"), "elapsed_seconds": elapsed,
              "conclusion": text.splitlines()[:lines],
              "pr_links": list(dict.fromkeys(link if link.startswith("http") else "https://" + link
                                              for link in links)),
              "artifacts": {"log": job.get("log") or str(jd / "log"),
                            "conclusion": conclusion,
                            "worktree": job.get("cwd")},
              "queue_reasons": job.get("queue_reasons", []),
              "last_checked": job.get("last_checked")}
    for field in ("pgid", "cancel_killed_pids", "cancel_signal"):
        if field in job:
            result[field] = job[field]
    if job.get("outcome_reason"):
        result["outcome_reason"] = job["outcome_reason"]
    if job.get("block_reasons"):
        result["block_reasons"] = job["block_reasons"]
    if job.get("base_sha") and job.get("cwd"):
        stat = git_output(job["cwd"], "diff", "--stat", job["base_sha"], "--", ".")
        names = git_output(job["cwd"], "diff", "--name-only", "-z", job["base_sha"], "--", ".")
        if stat is not None and names is not None:
            result["diff_stat"] = stat
            result["changed_files"] = [name for name in names.split("\0") if name]
    return result


def classify_outcome(exit_code, conclusion_path, job=None):
    """Exit evidence takes precedence; only a whole-line STATUS: BLOCKED blocks."""
    if exit_code is None or exit_code == 124:
        return "unknown", None
    if exit_code != 0:
        return "failed", None
    try:
        text = Path(conclusion_path).read_text(errors="replace") if conclusion_path else ""
    except OSError:
        text = ""
    if not text.strip():
        return "unknown", None
    if any(line.strip() == "STATUS: BLOCKED" for line in text.splitlines()):
        return "blocked", ["STATUS: BLOCKED"]
    return "done", None


def reclaim(root):
    n = 0
    for job in load_jobs(root, "running"):
        job["last_checked"] = datetime.now(TZ).isoformat()
        alive = False
        if job.get("pid"):
            try:
                os.kill(int(job["pid"]), 0)
                alive = True
            except (OSError, ValueError, TypeError):
                pass
        exit_p = remote_dir(job["id"]) / "exit"
        if alive and not exit_p.exists():
            write_job(root, "running", job)
            continue
        try:
            job["exit"] = int(exit_p.read_text().strip())
        except (OSError, ValueError):
            job["exit"] = None
        conc = remote_dir(job["id"]) / "conclusion.md"
        if not conc.exists():
            conc = conc.with_suffix(".txt")  # Already running legacy workers.
        job["conclusion"] = str(conc) if conc.exists() else None
        status, hits = classify_outcome(job["exit"], job["conclusion"], job)
        job["status"] = status
        if status == "unknown":
            if job["exit"] == 124:
                job["outcome_reason"] = "worker_timeout"
            elif job["exit"] is None:
                job["outcome_reason"] = "missing_or_invalid_exit"
            else:
                job["outcome_reason"] = "missing_conclusion"
        if hits:
            job["block_reasons"] = hits
        job["finished_at"] = datetime.now(TZ).isoformat()
        write_job(root, "done", job)
        remove_job(root, "running", job["id"])
        n += 1
    return n


def launch(root, job, pool):
    fake = os.environ.get("CS_TEST_FAKE_WORKER") == "1"
    if fake and (not (os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME"))
                 or home() == (Path.home() / ".coding-scheduler").resolve()):
        raise ValueError("CS_TEST_FAKE_WORKER requires an isolated CS_HOME")
    model, effort = model_for(pool, job.get("tier") or "M")
    model = job.get("model") or model
    jd = remote_dir(job["id"])
    jd.mkdir(parents=True, exist_ok=True)
    body = Path(job["prompt_file"]).read_text()
    prompt_on_disk = jd / "prompt.md"
    cwd = job.get("cwd") or str(Path.home())
    log, conclusion, exit_file = jd / "log", jd / "conclusion.md", jd / "exit"
    prompt_on_disk.write_text(NO_SUB + body + "\n\n" +
        "CONCLUSION OUTPUT RULE (overrides template paths): Write the final conclusion to " +
        str(conclusion) + ". Keep all conclusion*.md files outside the repository worktree; " +
        "do not create, stage, or commit a conclusion file inside the repository. " +
        "Research/read-only tasks deliver findings; no code commit, PR, merge, or deployment " +
        "is required unless the task explicitly requests it.\n")
    # A manually reused ID must not inherit old completion evidence.
    exit_file.unlink(missing_ok=True)
    conclusion.unlink(missing_ok=True)
    (jd / "conclusion.txt").unlink(missing_ok=True)
    base_sha = git_output(cwd, "rev-parse", "HEAD")
    job.update(pool=pool, model=model, effort=effort, base_sha=base_sha,
               started_at=datetime.now(TZ).isoformat(), conclusion=str(conclusion), log=str(log))
    # Fake worker is synchronous, requires an explicit isolated state root, and
    # never executes a caller-supplied command or a model CLI.
    if fake:
        conclusion.write_text("Fake worker completed.\n")
        log.write_text("test fake worker\n")
        exit_file.write_text("0\n")
        job.update(status="running", pid=None, pgid=None)
    else:
        q = shlex.quote
        if pool == "codex":
            command = ("codex exec --dangerously-bypass-approvals-and-sandbox -m %s "
                       "-c %s -C %s -o %s - < %s" %
                       (q(model), q("model_reasoning_effort=" + effort), q(cwd),
                        q(str(conclusion)), q(str(prompt_on_disk))))
        else:
            cc_effort = "high" if effort == "medium" else effort
            command = ("claude -p --dangerously-skip-permissions --model %s --effort %s "
                       '\"$(cat %s)\" > %s' %
                       (q(model), q(cc_effort), q(str(prompt_on_disk)), q(str(conclusion))))
        run_sh = jd / "run.sh"
        # EXIT trap records nonzero command/cd failures too (old set -e lost them).
        trap = ('rc=$?; printf "%s\\n" "$rc" > ' + q(str(exit_file) + ".tmp") +
                "; mv " + q(str(exit_file) + ".tmp") + " " + q(str(exit_file)))
        run_sh.write_text("#!/bin/bash\nset -euo pipefail\n" +
                          "trap " + q(trap) + " EXIT\n" +
                          "cd " + q(cwd) + "\n" + command + "\n")
        with log.open("w") as output:
            proc = subprocess.Popen(["bash", str(run_sh)], stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        job.update(status="running", pid=proc.pid, pgid=proc.pid)
        (jd / "pid").write_text(str(proc.pid) + "\n")
    write_job(root, "running", job)
    remove_job(root, "queue", job["id"])
    return job


def quota_reasons(root, pools):
    """Optional local snapshot. Absent file preserves legacy scheduling."""
    path = root / "quota.json"
    if not path.exists():
        return []
    try:
        quotas = json.loads(path.read_text())
        if not isinstance(quotas, dict):
            quotas = {}
    except (OSError, ValueError):
        quotas = {}
    reasons = []
    for pool in pools:
        value = quotas.get(pool)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            if value == 0:
                reasons.append({"type": "quota_exhausted", "pool": pool, "remaining": 0})
        else:
            reasons.append({"type": "quota_unknown", "pool": pool})
    return reasons


def check_queue(root, job, running, resolver=None):
    """Evaluate every independent blocker even if a lock already blocks launch."""
    conflicts = file_conflict(job, running, resolver)
    if conflicts:
        job["blocked_by"] = conflicts
    else:
        job.pop("blocked_by", None)
    c = counts(running)
    cs, ccs = score(job, c, area_counts(running))
    job["affinity"] = {"codex": cs, "cc": ccs}
    pool, reasons = pick_pool(cs, ccs, CODEX_CAP - c["codex"], CC_CAP - c["cc"])
    reasons = [dict(item, type="lock") for item in conflicts] + reasons
    # Quota applies to the selected pool; if capacity prevents selection, report
    # all affinity-eligible pools, so a pool failure does not hide quota failures.
    primary = "codex" if cs >= ccs else "cc"
    secondary = "cc" if primary == "codex" else "codex"
    eligible = [pool] if pool else [primary] + ([secondary] if job["affinity"][secondary] >= 45 else [])
    reasons += quota_reasons(root, eligible)
    job["queue_reasons"] = reasons
    job["last_checked"] = datetime.now(TZ).isoformat()
    return pool


def reason_text(reasons):
    texts = []
    for reason in reasons:
        kind = reason.get("type")
        if kind == "lock":
            text = "%s 占用 %s (%s锁)" % (reason["job"], reason["mine"], reason["kind"])
        elif kind == "pool_full":
            text = "%s 池满" % reason["pool"]
        elif kind == "spill_insufficient":
            text = "%s 副池分数 %s<%s" % (reason["pool"], reason["score"], reason["minimum"])
        elif kind == "quota_unknown":
            text = "%s 额度未知" % reason["pool"]
        elif kind == "quota_exhausted":
            text = "%s 额度耗尽" % reason["pool"]
        elif kind == "awaiting_tick":
            text = "等待 tick 调度"
        elif kind == "launch_error":
            text = "启动失败：" + reason["detail"]
        else:
            text = str(kind)
        if text not in texts:
            texts.append(text)
    return "；".join(texts) or "—"


def tick(root, resolver=None):
    root = ensure(root)
    resolver = resolver if resolver is not None else GitLockResolver()
    reclaimed = reclaim(root)
    running = load_jobs(root, "running")
    queued = load_jobs(root, "queue")
    c = counts(running)
    started = []

    def sk(j):
        return (
            0 if j.get("pin") else 1,
            {"H": 0, "M": 1, "L": 2}.get(j.get("tier") or "M", 1),
            j.get("created_at") or "",
        )

    for job in sorted(queued, key=sk):
        pool = check_queue(root, job, running, resolver)
        if job["queue_reasons"]:
            write_job(root, "queue", job)
            continue
        try:
            launch(root, job, pool)
        except (OSError, ValueError) as exc:
            job["queue_reasons"] = [{"type": "launch_error", "detail": str(exc)}]
            write_job(root, "queue", job)
            continue
        started.append(job["id"])
        running = load_jobs(root, "running")
        c = counts(running)

    write_board(root)
    result = {"reclaimed": reclaimed, "started": started, "counts": c,
            "queued": [{"id": j["id"], "queue_reasons": j.get("queue_reasons", []),
                        "last_checked": j.get("last_checked")} for j in load_jobs(root, "queue")]}
    log_tick(root, result)
    return result


def log_tick(root, result):
    """One bounded line per tick; rotate redirected launchd stdout in place too."""
    def limit(name, default, minimum):
        try:
            return max(minimum, int(os.environ.get(name, default)))
        except ValueError:
            return default

    max_bytes = limit("CS_TICK_LOG_MAX_BYTES", 1024 * 1024, 256)
    backups = limit("CS_TICK_LOG_BACKUPS", 3, 0)
    path = root / "logs" / "tick.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"at": datetime.now(TZ).isoformat(), "reclaimed": result["reclaimed"],
                       "started_count": len(result["started"]), "counts": result["counts"],
                       "queued_count": len(result["queued"])}) + "\n"
    if path.exists() and path.stat().st_size + len(line.encode()) > max_bytes:
        # Remove backups above a reduced retention setting as well.
        for old in path.parent.glob("tick.log.*"):
            if old.suffix[1:].isdigit() and int(old.suffix[1:]) > backups:
                old.unlink()
        if backups:
            for number in range(backups, 1, -1):
                previous = path.with_name("tick.log.%s" % (number - 1))
                if previous.exists():
                    previous.replace(path.with_name("tick.log.%s" % number))
            # Preserve the inode: launchd may already have stdout/stderr open.
            with path.open("rb") as source, path.with_name("tick.log.1").open("wb") as target:
                source.seek(max(0, path.stat().st_size - max_bytes))
                shutil.copyfileobj(source, target)
        with path.open("r+b") as handle:
            handle.truncate(0)
    try:
        redirected = path.exists() and os.path.samestat(os.fstat(sys.stdout.fileno()), path.stat())
    except (AttributeError, OSError, ValueError):
        redirected = False
    if not redirected:
        with path.open("a") as handle:
            handle.write(line)


def write_board(root):
    running = load_jobs(root, "running")
    queued = load_jobs(root, "queue")
    c = counts(running)
    lines = [
        "# Coding dispatch board (cs CLI)",
        "",
        "更新：%s UTC+8" % datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "运行计数：Codex `%s/%s` · CC `%s/%s`" % (c["codex"], CODEX_CAP, c["cc"], CC_CAP),
        "",
        "| id | project | area | tier | pool | status | cwd | 原因 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    rows = running + queued
    if not rows:
        lines.append("| — | — | — | — | — | idle | — | — |")
    for j in rows:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | `%s` | %s |"
            % (
                j.get("id", ""),
                j.get("project", ""),
                j.get("area", ""),
                j.get("tier", ""),
                j.get("pool") or "-",
                j.get("status", ""),
                j.get("cwd", ""),
                reason_text(j.get("queue_reasons", [])).replace("|", "\\|").replace("\n", " "),
            )
        )
    lines.append("")
    rules = root / "RULES.md"
    if rules.exists():
        extra = rules.read_text().strip()
        if extra:
            lines.append(extra)
            lines.append("")
    path = root / "board.md"
    path.write_text("\n".join(lines) + "\n")
    return path



def doctor_checks(cwd=None, pool=None):
    """Preflight for common hard-fail environments. Returns list of {name,ok,detail}."""
    import shutil
    import socket

    checks = []

    def add(name, ok, detail, required=False):
        checks.append({"name": name, "ok": bool(ok), "detail": detail,
                       "level": "OK" if ok else ("FAIL" if required else "WARN")})

    codex = shutil.which("codex")
    claude = shutil.which("claude")
    add("codex_cli", bool(codex), codex or "not on PATH", pool == "codex")
    add("claude_cli", bool(claude), claude or "not on PATH", pool == "cc")

    try:
        socket.getaddrinfo("github.com", 443)
        add("github_dns", True, "github.com resolves")
    except OSError as e:
        add("github_dns", False, str(e))

    if cwd:
        cpath = Path(cwd).expanduser()
        if not cpath.is_dir():
            add("cwd_exists", False, str(cpath), True)
            return checks
        add("cwd_exists", True, str(cpath))
        try:
            r = subprocess.run(
                ["git", "-C", str(cpath), "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode != 0:
                add("git_repo", False, (r.stderr or r.stdout or "not a git work tree").strip())
            else:
                gdir = Path(r.stdout.strip())
                if not gdir.is_absolute():
                    gdir = cpath / gdir
                probe = gdir / "cs-write-probe"
                try:
                    probe.write_text("ok\n")
                    probe.unlink(missing_ok=True)
                    add("git_writable", True, str(gdir))
                except OSError as e:
                    add("git_writable", False, str(e), True)
        except Exception as e:
            add("git_writable", False, str(e))

        trust = Path.home() / ".claude.json"
        if trust.exists():
            try:
                config = json.loads(trust.read_text(errors="replace"))
                projects = config.get("projects", {}) if isinstance(config, dict) else {}
                ok = isinstance(projects, dict) and any(
                    isinstance(value, dict) and value.get("hasTrustDialogAccepted") is True
                    and Path(path).expanduser().resolve() == cpath.resolve()
                    for path, value in projects.items())
                add(
                    "claude_trusted_dir_hint",
                    ok,
                    "trust accepted for cwd in ~/.claude.json" if ok else
                    "cwd trust not confirmed in ~/.claude.json; advisory only (worker uses --dangerously-skip-permissions)",
                )
            except (OSError, ValueError) as e:
                add("claude_trusted_dir_hint", False, str(e))
        else:
            add("claude_trusted_dir_hint", False, "~/.claude.json missing")

    return checks
