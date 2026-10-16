"""Queue / launch / board helpers for cs CLI."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from affinity import CC_CAP, CODEX_CAP, model_for, pick_pool, score

TZ = timezone(timedelta(hours=8))
NO_SUB = (
    "HARD RULE: do not spawn sub-agents, nested agents, or background agent workers. "
    "Stay single-threaded in this session.\n\n"
)


def home():
    return Path(os.environ.get("CODING_SCHEDULER_HOME", Path.home() / ".coding-scheduler"))


def ensure(root=None):
    root = root or home()
    for n in ("queue", "running", "done"):
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
    path = root / folder / ("%s.json" % job["id"])
    path.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n")


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


def file_conflict(job, running):
    mine = [f.rstrip("/") for f in (job.get("files") or [])]
    if not mine:
        return False
    for other in running:
        for f in other.get("files") or []:
            f = f.rstrip("/")
            for m in mine:
                if f == m or f.startswith(m + "/") or m.startswith(f + "/"):
                    return True
    return False


def remote_dir(jid):
    return Path.home() / ".remote-jobs" / jid

# Conclusion phrases that mean "process exited 0 but work was not delivered".
BLOCKED_MARKERS = (
    "未推送",
    "尚未推送",
    "未提交",
    "未开 pr",
    "未合并",
    "未修改 pr",
    "沙箱",
    "只读",
    "read-only",
    "readonly",
    "github 无法",
    "github api",
    "dns",
    "trusted directory",
    "trusted-dir",
    "permission denied",
    "需要审批",
    "策略为 never",
    "禁止提权",
    "仍受阻",
    "需在允许写入",
    "修复尚未提交",
    "也未提交",
    "未启动子",
)


def classify_outcome(exit_code, conclusion_path):
    """Map worker exit + conclusion text to done | failed | blocked."""
    if exit_code != 0:
        return "failed", None
    if not conclusion_path or not Path(conclusion_path).exists():
        return "done", None
    try:
        text = Path(conclusion_path).read_text(errors="replace")
    except OSError:
        return "done", None
    low = text.lower()
    hits = [m for m in BLOCKED_MARKERS if m.lower() in low]
    if hits:
        return "blocked", hits[:5]
    return "done", None


def reclaim(root):
    n = 0
    for job in load_jobs(root, "running"):
        pid = job.get("pid")
        alive = False
        if pid:
            try:
                os.kill(int(pid), 0)
                alive = True
            except OSError:
                alive = False
        exit_p = remote_dir(job["id"]) / "exit"
        if alive and not exit_p.exists():
            continue
        if exit_p.exists():
            try:
                job["exit"] = int(exit_p.read_text().strip() or "1")
            except ValueError:
                job["exit"] = 1
        else:
            job["exit"] = 1
        conc = remote_dir(job["id"]) / "conclusion.txt"
        job["conclusion"] = str(conc) if conc.exists() else None
        status, hits = classify_outcome(job.get("exit"), job.get("conclusion"))
        job["status"] = status
        if hits:
            job["block_reasons"] = hits
        job["finished_at"] = datetime.now(TZ).isoformat()
        remove_job(root, "running", job["id"])
        write_job(root, "done", job)
        n += 1
    return n


def launch(root, job, pool):
    model, effort = model_for(pool, job.get("tier") or "M")
    jd = remote_dir(job["id"])
    jd.mkdir(parents=True, exist_ok=True)
    prompt_path = Path(job["prompt_file"])
    body = prompt_path.read_text() if prompt_path.exists() else ""
    prompt = NO_SUB + body
    (jd / "prompt.md").write_text(prompt)
    cwd = job.get("cwd") or str(Path.home())
    log = jd / "log"
    conclusion = jd / "conclusion.txt"
    exit_file = jd / "exit"

    # Feed prompt from jd/prompt.md via a tiny on-disk run.sh so the outer
    # nohup/argv never embeds the body (nested json.dumps caused R75-p1 ARG_MAX).
    prompt_on_disk = jd / "prompt.md"
    run_sh = jd / "run.sh"
    if pool == "codex":
        run_sh.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            "cd %s\n"
            "codex exec --dangerously-bypass-approvals-and-sandbox -m %s -c model_reasoning_effort=%s -C %s -o %s - < %s\n"
            "echo $? > %s\n"
            % (
                json.dumps(cwd),
                model,
                json.dumps(effort),
                json.dumps(cwd),
                json.dumps(str(conclusion)),
                json.dumps(str(prompt_on_disk)),
                json.dumps(str(exit_file)),
            )
        )
    else:
        # murmurant native verify rejects sonnet/opus effort=medium + thinking=adaptive;
        # verified includes high+adaptive. Coerce medium→high until R91 deploys.
        cc_effort = "high" if effort == "medium" else effort
        # Claude has no stdin-prompt mode that skips argv; keep body in a second
        # file and expand inside run.sh (still not on the outer nohup argv).
        run_sh.write_text(
            "#!/bin/bash\nset -euo pipefail\n"
            "cd %s\n"
            "PROMPT=$(cat %s)\n"
            "claude -p --dangerously-skip-permissions --model %s --effort %s \"$PROMPT\" > %s 2>> %s\n"
            "echo $? > %s\n"
            % (
                json.dumps(cwd),
                json.dumps(str(prompt_on_disk)),
                model,
                cc_effort,
                json.dumps(str(conclusion)),
                json.dumps(str(log)),
                json.dumps(str(exit_file)),
            )
        )
    run_sh.chmod(0o755)
    full = "nohup %s > %s 2>&1 & echo $!" % (json.dumps(str(run_sh)), json.dumps(str(log)))
    proc = subprocess.run(["bash", "-lc", full], capture_output=True, text=True)
    lines = (proc.stdout or "").strip().splitlines()
    try:
        pid = int(lines[-1]) if lines else None
    except ValueError:
        pid = None
    if not pid:
        job["status"] = "failed"
        job["exit"] = 1
        job["finished_at"] = datetime.now(TZ).isoformat()
        remove_job(root, "queue", job["id"])
        write_job(root, "done", job)
        return job

    (jd / "pid").write_text(str(pid) + "\n")
    job["pool"] = pool
    job["pid"] = pid
    job["status"] = "running"
    job["model"] = model
    job["effort"] = effort
    job["started_at"] = datetime.now(TZ).isoformat()
    job["conclusion"] = str(conclusion)
    remove_job(root, "queue", job["id"])
    write_job(root, "running", job)
    return job


def tick(root):
    root = ensure(root)
    reclaimed = reclaim(root)
    running = load_jobs(root, "running")
    queued = load_jobs(root, "queue")
    c = counts(running)
    by_area = area_counts(running)
    started = []

    def sk(j):
        return (
            0 if j.get("pin") else 1,
            {"H": 0, "M": 1, "L": 2}.get(j.get("tier") or "M", 1),
            j.get("created_at") or "",
        )

    for job in sorted(queued, key=sk):
        if file_conflict(job, running):
            continue
        cs, ccs = score(job, c, by_area)
        job["affinity"] = {"codex": cs, "cc": ccs}
        pool = pick_pool(cs, ccs, CODEX_CAP - c["codex"], CC_CAP - c["cc"])
        if not pool:
            write_job(root, "queue", job)
            continue
        launch(root, job, pool)
        started.append(job["id"])
        running = load_jobs(root, "running")
        c = counts(running)
        by_area = area_counts(running)

    write_board(root)
    return {"reclaimed": reclaimed, "started": started, "counts": c}


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
        "| id | project | area | tier | pool | status | cwd |",
        "|---|---|---|---|---|---|---|",
    ]
    rows = running + queued
    if not rows:
        lines.append("| — | — | — | — | — | idle | — |")
    for j in rows:
        lines.append(
            "| %s | %s | %s | %s | %s | %s | `%s` |"
            % (
                j.get("id", ""),
                j.get("project", ""),
                j.get("area", ""),
                j.get("tier", ""),
                j.get("pool") or "-",
                j.get("status", ""),
                j.get("cwd", ""),
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



def doctor_checks(cwd=None):
    """Preflight for common hard-fail environments. Returns list of {name,ok,detail}."""
    import shutil
    import socket

    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    codex = shutil.which("codex")
    claude = shutil.which("claude")
    add("codex_cli", bool(codex), codex or "not on PATH")
    add("claude_cli", bool(claude), claude or "not on PATH")

    try:
        socket.getaddrinfo("github.com", 443)
        add("github_dns", True, "github.com resolves")
    except OSError as e:
        add("github_dns", False, str(e))

    if cwd:
        cpath = Path(cwd).expanduser()
        if not cpath.exists():
            add("cwd_exists", False, str(cpath))
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
                    add("git_writable", False, str(e))
        except Exception as e:
            add("git_writable", False, str(e))

        trust = Path.home() / ".claude.json"
        if trust.exists():
            try:
                raw = trust.read_text(errors="replace")
                ok = str(cpath) in raw or str(cpath.resolve()) in raw
                add(
                    "claude_trusted_dir_hint",
                    ok,
                    "path listed in ~/.claude.json" if ok else "path not listed in ~/.claude.json (may prompt/fail)",
                )
            except OSError as e:
                add("claude_trusted_dir_hint", False, str(e))
        else:
            add("claude_trusted_dir_hint", False, "~/.claude.json missing")

    return checks
