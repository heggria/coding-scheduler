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
import uuid
from contextlib import contextmanager
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import affinity
from affinity import CC_CAP, CODEX_CAP, OPENCODE_CAP, model_for, pick_pool, score
from locks import GitLockResolver, file_conflict, lock_context
from scope import narrow_warnings, policy_text, scope_report
from stats import conclusion_text, timestamp, upstream_transient

TZ = timezone(timedelta(hours=8))
NO_SUB = (
    "HARD RULE: do not spawn sub-agents, nested agents, or background agent workers. "
    "Stay single-threaded in this session.\n\n"
)


def home():
    return Path(os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME")
                or Path.home() / ".coding-scheduler").expanduser().resolve()


_LOADED_CAPS = {"codex": CODEX_CAP, "cc": CC_CAP, "opencode": OPENCODE_CAP}


def reload_caps(root=None):
    """Re-read caps (caps.json > env > defaults) into affinity and core globals.

    A core global that was overridden since the last reload (e.g. a test patch)
    is left alone so explicit assignments keep winning.
    """
    caps = affinity.reload_caps(root if root is not None else home())
    g = globals()
    for key, name in (("codex", "CODEX_CAP"), ("cc", "CC_CAP"), ("opencode", "OPENCODE_CAP")):
        if g[name] == _LOADED_CAPS[key]:
            g[name] = caps[key]
        _LOADED_CAPS[key] = caps[key]
    return {key: g[key.upper() + "_CAP"] for key in caps}


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
    c = {"codex": 0, "cc": 0, "opencode": 0}
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


def submission_prompt(root, job):
    """Rebuild declarations from submission fields, including for retries."""
    protected = protected_scope_paths(job, load_jobs(root, "running"))
    declaration = "\n".join([
        '## 本单声明',
        '- cwd: ' + job["cwd"],
        '- 可写文件清单 (files): ' + (json.dumps(job["files"], ensure_ascii=False) if job["files"] else '无'),
        '- shared 锁文件 (shared_files): ' + (json.dumps(job["shared_files"], ensure_ascii=False) if job["shared_files"] else '无'),
        '- area: ' + job["area"],
        '- read-only: ' + ('是' if job["read_only"] else '否'),
        '', policy_text(job["scope_policy"], protected),
    ])
    body = declaration + "\n\n" + job["user_prompt"]
    template = root / "projects" / (job["project"] + ".md")
    if not job.get("no_template") and template.is_file():
        body = template.read_text().rstrip() + "\n\n" + body
    return body


def retry_transient(root, original):
    """Queue one fresh attempt; never inherit execution/terminal evidence."""
    if original.get("retry_of"):
        return None
    legacy_prompt = None
    if "user_prompt" not in original:
        try:
            legacy_prompt = Path(original["prompt_file"]).read_text()
        except (KeyError, OSError, TypeError):
            original["retry_error"] = "original prompt unavailable; submit a new job explicitly"
            return None
    fields = ("project", "area", "tier", "cwd", "risk", "pin", "read_only", "files",
              "shared_files", "no_template", "user_prompt", "review_of")
    job = {key: original[key] for key in fields if key in original}
    job.setdefault("files", [])
    job.setdefault("shared_files", [])
    job.setdefault("read_only", False)
    job.update(id=original["id"] + "-retry-" + uuid.uuid4().hex[:8], retry_of=original["id"],
               created_at=datetime.now(TZ).isoformat(), status="queued", pool=None, pid=None,
               conclusion=None, exit=None, idempotency_key=None, affinity={},
               scope_policy=original.get("scope_policy", "strict"),
               model=original.get("requested_model", original.get("model")))
    job["requested_model"] = job["model"]
    job["warnings"] = narrow_warnings(job["files"], job["scope_policy"], job["shared_files"])
    prompt = root / "queue" / (job["id"] + ".prompt.md")
    prompt.write_text(legacy_prompt if legacy_prompt is not None else submission_prompt(root, job))
    job["prompt_file"] = str(prompt)
    job["lock_ctx"] = lock_context(job)
    check_queue(root, job, [j for j in load_jobs(root, "running") if j["id"] != original["id"]])
    job["queue_reasons"].append({"type": "awaiting_tick"})
    write_job(root, "queue", job)
    return job["id"]


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
    for field in ("pgid", "cancel_killed_pids", "cancel_signal", "scope_policy", "scope",
                  "changed_files", "need_files", "retry_of", "retried_as", "raw_status", "warnings",
                  "failure_reason", "failure_detail", "retry_error", "last_queue_reasons", "queue_wait_s"):
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
            if "changed_files" not in job:
                result["changed_files"] = [name for name in names.split("\0") if name]
    return result


def classify_outcome(exit_code, conclusion_path, job=None):
    """Exit evidence takes precedence; preserve a delivered BLOCKED as a warning."""
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
        if delivered(text):
            if job is not None:
                job["raw_status"] = "blocked"
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                marker = lines.index("STATUS: BLOCKED")
                reason = next((line for line in lines[marker + 1:] if not line.startswith("STATUS:")),
                              next((line for line in lines if not line.startswith("STATUS:")), "STATUS: BLOCKED"))
                warning = "delivered_but_blocked: " + reason[:240]
                job["warnings"] = list(dict.fromkeys((job.get("warnings") or []) + [warning]))
            return "done", None
        return "blocked", ["STATUS: BLOCKED"]
    return "done", None


def delivered(text):
    negative = (r"no code changes|no commits?|\bno\s+(?:draft\s+)?pr\b|not (?:created|pushed|committed)|"
                r"(?:did not|have not|haven't|could not|couldn't) (?:push|commit|create|open)|"
                r"未改代码|未提交|未推送|未开\s*pr|未创建\s*pr")
    has_negative = re.search(negative, text, re.I)
    for line in re.split(r"[\n；;。]", text):
        if re.search(negative, line, re.I):
            continue
        if re.search(r"https?://[^\s<>]+/pulls?/\d+\b", line, re.I):
            return True
        if re.search(r"\bpr\s*#\d+\b", line, re.I) and re.search(r"created|draft|已开|创建", line, re.I):
            return True
    for sha in re.finditer(r"\b[0-9a-f]{40}\b", text, re.I):
        nearby = text[max(0, sha.start() - 100):sha.end() + 100]
        if re.search(negative, nearby, re.I):
            continue
        if re.search(r"pushed|推送", nearby, re.I):
            return True
        if not has_negative and re.search(r"\b(?:head|commit)\b", nearby, re.I):
            return True
    return False


def opencode_idle(job, now):
    """Persist both files' mtime/size, including same-size rewrites and deletion."""
    jd = remote_dir(job["id"])
    signature, mtimes = [], []
    for path in (Path(job.get("log") or jd / "log"), jd / "output.jsonl"):
        try:
            stat = path.stat()
            signature.append([stat.st_mtime_ns, stat.st_size])
            mtimes.append(stat.st_mtime)
        except OSError:
            signature.append(None)
    start = timestamp(job.get("started_at"))
    last = job.get("output_changed_at", max([start.timestamp() if start else now] + mtimes))
    if "output_signature" in job and signature != job["output_signature"]:
        changes = [current[0] / 1e9 if current and (previous is None or current[0] != previous[0]) else now
                   for previous, current in zip(job["output_signature"], signature) if previous != current]
        last = max([last] + changes)
    job.update(output_signature=signature, output_changed_at=last)
    try:
        threshold = float(os.environ.get("CS_OPENCODE_IDLE_S", "600"))
        if not math.isfinite(threshold) or threshold <= 0:
            threshold = 600
    except ValueError:
        threshold = 600
    return now - last >= threshold


def failure_text(job):
    """Bound log reads; conclusions and job-local JSON errors are sufficient."""
    parts = [conclusion_text(job)]
    jd = remote_dir(job["id"])
    for path in (Path(job.get("log") or jd / "log"), jd / "output.jsonl"):
        try:
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 65536))
                parts.append(handle.read(65536).decode("utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(parts)


def opencode_failure(text):
    for reason, pattern in (
            ("insufficient_funds", r"insufficient account funds"),
            ("model_unavailable", r"model unavailable|provider[./]no-route"),
            ("auth", r"\b(?:401|403|unauthorized|forbidden)\b|not authenticated|"
                     r"invalid[ _-](?:api[ _-])?key|auth(?:entication|orization)?[ _-]*(?:error|fail(?:ed|ure)?|required)"),
            ("rate_limit", r"\b429\b|rate[ _-]limit"),
            ("opencode_error", r'"type"\s*:\s*"error"')):
        match = re.search(pattern, text, re.I)
        if match:
            return reason, " ".join(text[max(0, match.start() - 40):match.end() + 160].split())[:240]
    return None, None


def protected_scope_paths(job, running):
    """Map other jobs' same-repo shared locks into this job's cwd coordinates."""
    resolver = GitLockResolver()
    cwd = Path(job["cwd"]).resolve()
    repo = resolver(cwd)
    paths = []
    for other in running:
        if other["id"] == job["id"]:
            continue
        for lock in lock_context(other, resolver)["locks"]:
            if lock["kind"] != "shared":
                continue
            if repo and lock["repo_id"] == repo[0]:
                target = Path(repo[1]) / lock["relpath"]
            elif not repo and lock["repo_id"] is None:
                target = Path(lock["relpath"])
            else:
                continue
            paths.append("." if cwd.is_relative_to(target) else os.path.relpath(str(target), str(cwd)))
    return list(dict.fromkeys(paths))


def completion_scope(job, running):
    """Best-effort repository snapshot, including untracked files; never enforce."""
    cwd = job.get("cwd")
    names = None
    if cwd and job.get("base_sha"):
        names = git_output(cwd, "diff", "--relative", "--no-renames", "--name-only", "-z",
                           job["base_sha"], "--", ".")
    changed = [name for name in (names or "").split("\0") if name]
    if names is not None:
        untracked = git_output(cwd, "ls-files", "--others", "--exclude-standard", "-z", "--", ".")
        changed.extend(name for name in (untracked or "").split("\0") if name)
    job["changed_files"] = sorted(set(changed))
    declared = [os.path.relpath(str((Path(cwd) / path).resolve()), cwd)
                for path in (job.get("files") or []) + (job.get("shared_files") or [])] if cwd else []
    job["scope"] = scope_report(job["changed_files"], declared,
                                 protected_scope_paths(job, running) if cwd else [],
                                 job.get("scope_policy", "strict"))


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
        idle_timeout = (alive and not exit_p.exists() and job.get("pool") == "opencode"
                        and opencode_idle(job, time.time()))
        if idle_timeout:
            job.update(kill_worker_group(job.get("pgid") or job["pid"]))
            job["failure_reason"] = "no_output_timeout"
            job["failure_detail"] = "No job-local output change for %.0f seconds" % (
                time.time() - job["output_changed_at"])
        if alive and not exit_p.exists() and not idle_timeout:
            write_job(root, "running", job)
            continue
        try:
            job["exit"] = int(exit_p.read_text().strip())
        except (OSError, ValueError):
            job["exit"] = None
        if idle_timeout:
            job["exit"] = 1
        conc = remote_dir(job["id"]) / "conclusion.md"
        if not conc.exists():
            conc = conc.with_suffix(".txt")  # Already running legacy workers.
        job["conclusion"] = str(conc) if conc.exists() else None
        status, hits = classify_outcome(job["exit"], job["conclusion"], job)
        job["status"] = status
        if status == "failed" or job["exit"] == 124:
            text = failure_text(job)
            reason = None
            if job.get("pool") == "opencode":
                reason, detail = opencode_failure(text)
                if reason:
                    if not idle_timeout:
                        job["failure_reason"] = reason
                    job["failure_detail"] = detail
            if (status == "failed" and not idle_timeout and reason in (None, "opencode_error")
                    and upstream_transient(text)):
                job["failure_reason"] = "upstream_transient"
                job["failure_detail"] = next((line.strip()[:240] for line in text.splitlines()
                                              if upstream_transient(line)), "upstream transient error")
                retried_as = retry_transient(root, job)
                if retried_as:
                    job["retried_as"] = retried_as
        if status == "unknown":
            if job["exit"] == 124:
                job["outcome_reason"] = "worker_timeout"
            elif job["exit"] is None:
                job["outcome_reason"] = "missing_or_invalid_exit"
            else:
                job["outcome_reason"] = "missing_conclusion"
        if hits:
            job["block_reasons"] = hits
        if status == "blocked" and job["conclusion"]:
            try:
                text = Path(job["conclusion"]).read_text(errors="replace")
            except OSError:
                text = ""
            job["need_files"] = list(dict.fromkeys(
                path.strip() for line in text.splitlines() if line.strip().startswith("NEED_FILES:")
                for path in line.strip()[len("NEED_FILES:"):].split(",") if path.strip()))
        completion_scope(job, load_jobs(root, "running"))
        job["finished_at"] = datetime.now(TZ).isoformat()
        write_job(root, "done", job)
        remove_job(root, "running", job["id"])
        log_event(root, job)
        n += 1
    return n


def validate_model(pool, model):
    if pool == "opencode" and model and not model.startswith(("opencode/", "opencode-go/")):
        raise ValueError("opencode pool requires an --model with opencode/ or opencode-go/ prefix")


def extract_opencode_text(source, destination):
    """Extract only JSONL text parts; metadata/tool events stay in the worker log."""
    with Path(source).open() as stream, Path(destination).open("w") as output:
        for line in stream:
            event = json.loads(line)
            if not isinstance(event, dict) or event.get("type") != "text":
                continue
            part = event.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                output.write(part["text"])


def launch(root, job, pool):
    fake = os.environ.get("CS_TEST_FAKE_WORKER") == "1"
    if fake and (not (os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME"))
                 or home() == (Path.home() / ".coding-scheduler").resolve()):
        raise ValueError("CS_TEST_FAKE_WORKER requires an isolated CS_HOME")
    model, effort = model_for(pool, job.get("tier") or "M")
    model = job.get("model") or model
    validate_model(pool, model)
    jd = remote_dir(job["id"])
    jd.mkdir(parents=True, exist_ok=True)
    body = Path(job["prompt_file"]).read_text()
    prompt_on_disk = jd / "prompt.md"
    cwd = job.get("cwd") or str(Path.home())
    log, conclusion, exit_file = jd / "log", jd / "conclusion.md", jd / "exit"
    prompt_on_disk.write_text(NO_SUB + body + "\n\n" +
        "Tests or CI checks already failing on the base branch/main are not blockers for this job. " +
        "Verify by running the same checks on base, list them under a 'Pre-existing failures' " +
        "note in the conclusion, and still deliver.\n\n" +
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
    created = timestamp(job.get("created_at"))
    if created:
        job["queue_wait_s"] = round(max(0, (timestamp(job["started_at"]) - created).total_seconds()), 3)
    if job.get("queue_reasons"):
        job["last_queue_reasons"] = job["queue_reasons"]
    job["queue_reasons"] = []
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
        elif pool == "opencode":
            events = jd / "output.jsonl"
            # Read the prompt from disk, and keep stdout JSONL separate from stderr.
            extract = ("import sys; sys.path.insert(0, %r); "
                       "from core import extract_opencode_text; "
                       "extract_opencode_text(sys.argv[1], sys.argv[2])" % str(Path(__file__).resolve().parent))
            # OpenCode 2.x: variant is part of the model id; run.sh already cd's into cwd.
            command = ("opencode run -m %s --auto --format json "
                       '\"$(cat %s)\" | tee %s\n/usr/bin/python3 -c %s %s %s' %
                       (q(model + "#max"), q(str(prompt_on_disk)), q(str(events)),
                        q(extract), q(str(events)), q(str(conclusion))))
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
                          # Workers inherit CS_JOB_ID so nested agents can detect cs jobs.
                          "export CS_JOB_ID=" + q(job["id"]) + "\n" +
                          "cd " + q(cwd) + "\n" + command + "\n")
        with log.open("w") as output:
            # Detach stdin: opencode run reads a non-TTY stdin and blocks forever on it.
            proc = subprocess.Popen(["bash", str(run_sh)], stdin=subprocess.DEVNULL, stdout=output,
                                    stderr=subprocess.STDOUT, start_new_session=True)
        job.update(status="running", pid=proc.pid, pgid=proc.pid)
        (jd / "pid").write_text(str(proc.pid) + "\n")
    if pool == "opencode":
        opencode_idle(job, time.time())
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
        if pool == "opencode":
            continue
        value = quotas.get(pool)
        if (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0):
            if value == 0:
                reasons.append({"type": "quota_exhausted", "pool": pool, "remaining": 0})
        else:
            reasons.append({"type": "quota_unknown", "pool": pool})
    return reasons


def low_risk(job, resolver=None):
    if job.get("area") == "research" and job.get("read_only"):
        return True
    if len(job.get("files") or []) > 3 or job.get("shared_files") or job.get("risk") == "high":
        return False
    for entry in lock_context(job, resolver)["locks"]:
        relative = entry["relpath"]
        if entry["worktree_root"] is None:
            # Outside Git, use cwd-relative names instead of the absolute fallback.
            relative = os.path.relpath(relative, job.get("cwd") or str(Path.home()))
        if relative.startswith(("lib/core.py", "lib/affinity.py", "bin/", "launchd/")):
            return False
        if relative in (".", "lib", "bin", "launchd"):
            return False
    return True


def check_queue(root, job, running, resolver=None):
    """Evaluate every independent blocker even if a lock already blocks launch."""
    reload_caps(root)
    resolver = resolver if resolver is not None else GitLockResolver()
    conflicts = file_conflict(job, running, resolver)
    if conflicts:
        job["blocked_by"] = conflicts
    else:
        job.pop("blocked_by", None)
    c = counts(running)
    cs, ccs = score(job, c, area_counts(running))
    job["affinity"] = {"codex": cs, "cc": ccs}
    pin = job.get("pin")
    if pin == "opencode":
        pool = "opencode" if c["opencode"] < OPENCODE_CAP else None
        reasons = [] if pool else [{"type": "pool_full", "pool": "opencode",
                                   "free": OPENCODE_CAP - c["opencode"]}]
        eligible = ["opencode"]
    elif (not pin and job.get("tier") == "L" and low_risk(job, resolver)
          and c["opencode"] < OPENCODE_CAP):
        pool, reasons, eligible = "opencode", [], ["opencode"]
    else:
        pool, reasons = pick_pool(cs, ccs, CODEX_CAP - c["codex"], CC_CAP - c["cc"])
        primary = "codex" if cs >= ccs else "cc"
        secondary = "cc" if primary == "codex" else "codex"
        eligible = [pool] if pool else [primary] + ([secondary] if job["affinity"][secondary] >= 45 else [])
    reasons = [dict(item, type="lock") for item in conflicts] + reasons
    # Quota applies to the selected pool; if capacity prevents selection, report
    # all affinity-eligible pools, so a pool failure does not hide quota failures.
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
    reload_caps(root)
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
        previous_reasons = job.get("queue_reasons") or []
        pool = check_queue(root, job, running, resolver)
        if job["queue_reasons"]:
            write_job(root, "queue", job)
            continue
        if previous_reasons:
            job["last_queue_reasons"] = previous_reasons
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


def append_log(root, filename, line, env_prefix, redirected_stdout=False, whole_lines=False):
    """Shared bounded rotation; preserve the inode for launchd's open stdout."""
    def limit(name, default, minimum):
        try:
            return max(minimum, int(os.environ.get(name, default)))
        except ValueError:
            return default

    max_bytes = limit(env_prefix + "_MAX_BYTES", 1024 * 1024, 256)
    backups = limit(env_prefix + "_BACKUPS", 3, 0)
    path = root / "logs" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size + len(line.encode()) > max_bytes:
        # Remove backups above a reduced retention setting as well.
        for old in path.parent.glob(filename + ".*"):
            if old.suffix[1:].isdigit() and int(old.suffix[1:]) > backups:
                old.unlink()
        if backups:
            for number in range(backups, 1, -1):
                previous = path.with_name(filename + ".%s" % (number - 1))
                if previous.exists():
                    previous.replace(path.with_name(filename + ".%s" % number))
            # Preserve the inode: launchd may already have stdout/stderr open.
            with path.open("rb") as source, path.with_name(filename + ".1").open("wb") as target:
                offset = max(0, path.stat().st_size - max_bytes)
                source.seek(offset)
                if whole_lines and offset:
                    source.seek(offset - 1)
                    if source.read(1) != b"\n":
                        source.readline()  # Never leave a partial JSON object in a backup.
                shutil.copyfileobj(source, target)
        with path.open("r+b") as handle:
            handle.truncate(0)
    try:
        redirected = redirected_stdout and path.exists() and os.path.samestat(os.fstat(sys.stdout.fileno()), path.stat())
    except (AttributeError, OSError, ValueError):
        redirected = False
    if not redirected:
        with path.open("a") as handle:
            handle.write(line)


def log_tick(root, result):
    """One bounded line per tick, including when launchd redirects stdout."""
    line = json.dumps({"at": datetime.now(TZ).isoformat(), "reclaimed": result["reclaimed"],
                       "started_count": len(result["started"]), "counts": result["counts"],
                       "queued_count": len(result["queued"])}) + "\n"
    append_log(root, "tick.log", line, "CS_TICK_LOG", redirected_stdout=True)


def log_event(root, job):
    """Call once after a serialized terminal transition; no outcome mutation."""
    from stats import job_event
    line = json.dumps(job_event(job), ensure_ascii=False) + "\n"
    append_log(root, "events.jsonl", line, "CS_EVENT_LOG", whole_lines=True)


def write_board(root):
    reload_caps(root)
    running = load_jobs(root, "running")
    queued = load_jobs(root, "queue")
    c = counts(running)
    lines = [
        "# Coding dispatch board (cs CLI)",
        "",
        "更新：%s UTC+8" % datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
        "运行计数：Codex `%s/%s` · CC `%s/%s` · OpenCode `%s/%s`" %
        (c["codex"], CODEX_CAP, c["cc"], CC_CAP, c["opencode"], OPENCODE_CAP),
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
    opencode = shutil.which("opencode")
    add("codex_cli", bool(codex), codex or "not on PATH", pool == "codex")
    add("claude_cli", bool(claude), claude or "not on PATH", pool == "cc")
    add("opencode_cli", bool(opencode), opencode or "not on PATH", pool == "opencode")

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
