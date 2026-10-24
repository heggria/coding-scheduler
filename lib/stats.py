"""Read-only terminal-job statistics and the shared job-end event contract."""
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from narrow import NARROW_CLASSES, narrow_class

TZ = timezone(timedelta(hours=8))
TERMINAL = ("done", "blocked", "failed", "unknown", "cancelled")
POOLS = ("codex", "cc", "opencode")
# Outcome classes separate "the task did not get done" from noise that is not the task's fault.
OUTCOME_CLASSES = ("success", "task", "infra", "interrupted", "operator")
CANCEL_REASONS = ("operator", "superseded", "wrong-prompt", "batch-abort", "infra")
INFRA_FAILURES = ("upstream_transient", "rate_limit", "no_output_timeout", "stream_truncated",
                  "abrupt_exit", "opencode_error")


def timestamp(value):
    try:
        if isinstance(value, str) and value.endswith("Z"):
            value = value[:-1] + "+00:00"
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed
    except (TypeError, ValueError):
        return None


def parse_since(value="2h", now=None):
    now = now or datetime.now(TZ)
    match = re.fullmatch(r"([0-9]+)([mhd])", value)
    if match:
        try:
            return now - timedelta(seconds=int(match[1]) * {"m": 60, "h": 3600, "d": 86400}[match[2]])
        except OverflowError:
            pass
    else:
        parsed = timestamp(value)
        if parsed:
            return parsed
    raise ValueError("since must be a duration (2h, 30m, 1d) or ISO timestamp")


def conclusion_text(job):
    try:
        return Path(job["conclusion"]).read_text(errors="replace") if job.get("conclusion") else ""
    except OSError:
        return ""


def upstream_transient(text):
    return bool(re.search(
        r"transport channel closed|error sending request for url \(https://chatgpt\.com|"
        r"model is at capacity|http(?:/\S+)?\s+(?:502|503|504)\b|api error:\s*50[0-9]\b|"
        r"bad gateway|service unavailable|connection reset", text, re.I))


def blocked_class(job, text):
    if job.get("status") != "blocked":
        return None
    if job.get("need_files"):
        return "files_narrow"
    text = (text + "\n" + str(job.get("block_reasons", ""))).lower()
    # Explicit causes take precedence over mentions of already delivered artifacts.
    if re.search(r"read.only|只读|不能写|不可写", text) and (
            job.get("read_only") or re.search(r"落盘|写入|写文件|write|output|deliver", text)):
        return "read_only_write"
    if re.search(r"清单外|列表外|清单.*(?:缺|窄|未包|未涵盖)|不在.*(?:files|清单|列表)|"
                 r"未授权.*文件|文件.*未授权|files.*(?:narrow|allow|list)|"
                 r"(?:outside|missing|expand|additional).*?(?:files|writable)|需追加.*路径|"
                 r"需(?:追加|额外|新增)授权|未获写授权|缺少必要文件授权|不在白名单|"
                 r"outside the (?:allowed|writable) scope|need_files", text):
        return "files_narrow"
    if re.search(r"also reproduced on (?:origin/)?main|main\s*上同样失败|"
                 r"pre-existing failure|已在\s*main\s*失败", text):
        return "pre_existing_main_failure"
    if upstream_transient(text):
        return "upstream_transient"
    if re.search(r"坏输入|路径.*(?:不存在|为空)|缺少输入|missing input|input.*(?:missing|invalid)|"
                 r"(?:path|file).*not (?:found|exist)", text):
        return "bad_input"
    if re.search(r"ci.*pending|pending.*ci|hold|已交付|评审已|pr.*(?:已开|created)|"
                 r"未合并|等待.*(?:ci|review)|review.*pending", text):
        return "soft_block"
    return "other"


def signaled_exit(code):
    return isinstance(code, int) and not isinstance(code, bool) and 128 < code < 160


def outcome_class(status, failure_reason=None, exit_code=None, blocked_reason=None):
    """success / task / infra / interrupted / operator; only `task` counts against the task."""
    if status == "done":
        return "success"
    if status == "cancelled":
        return "operator"  # only `cs cancel` produces cancelled
    if status == "failed":
        if failure_reason == "signaled" or (failure_reason is None and signaled_exit(exit_code)):
            return "interrupted"
        if failure_reason in INFRA_FAILURES:
            return "infra"
        return "task"
    if status == "blocked" and blocked_reason == "upstream_transient":
        return "infra"
    return "task"


def job_event(job):
    """Build an observation only; never classify or change the stored outcome."""
    start, end = timestamp(job.get("started_at")), timestamp(job.get("finished_at"))
    text = conclusion_text(job)
    try:
        prompt_bytes = Path(job["prompt_file"]).stat().st_size if job.get("prompt_file") else None
    except OSError:
        prompt_bytes = None
    reasons = job.get("last_queue_reasons") or job.get("queue_reasons") or []
    event = dict(at=job.get("finished_at"), id=job.get("id"), project=job.get("project"),
                pool=job.get("pool"), area=job.get("area"), tier=job.get("tier"),
                status=job.get("status"), exit=job.get("exit"),
                elapsed_s=round(max(0, (end - start).total_seconds()), 3) if start and end else None,
                prompt_bytes=prompt_bytes,
                queue_wait_s=job.get("queue_wait_s"),
                last_queue_reasons=job.get("last_queue_reasons") or [],
                queue_reason_types=list(dict.fromkeys(r["type"] for r in reasons
                                        if isinstance(r, dict) and isinstance(r.get("type"), str))),
                blocked_reason_class=blocked_class(job, text),
                failure_reason=job.get("failure_reason"),
                cancel_reason=cancel_reason(job),
                cancelled_after_done=job.get("status") == "cancelled" and
                any(line.strip() == "STATUS: DONE" for line in text.splitlines()))
    event["outcome_class"] = outcome_class(event["status"], event["failure_reason"], event["exit"],
                                           event["blocked_reason_class"])
    event["narrow_reason_class"] = ((job.get("narrow_reason_class") or narrow_class(job, text))
                                   if event["blocked_reason_class"] == "files_narrow" else None)
    return event


def cancel_reason(job):
    if job.get("status") != "cancelled":
        return None
    if job.get("cancel_reason"):
        return job["cancel_reason"]
    return "superseded" if job.get("superseded_by") else "unspecified"


def classify_event(event, job=None):
    """Old events predate outcome fields: borrow them from the done record when available."""
    event = dict(event)
    source = job if isinstance(job, dict) else event
    if "narrow_reason_class" not in event:
        event["narrow_reason_class"] = ((source.get("narrow_reason_class") or
                                         narrow_class(source, conclusion_text(source)))
            if event.get("blocked_reason_class") == "files_narrow" else None)
    if "outcome_class" in event:
        return event
    event.setdefault("failure_reason", source.get("failure_reason"))
    event.setdefault("cancel_reason", cancel_reason(dict(source, status=event.get("status"))))
    event["outcome_class"] = outcome_class(event.get("status"), event.get("failure_reason"),
                                           event.get("exit"), event.get("blocked_reason_class"))
    return event


def numbers(events, field):
    return sorted(e[field] for e in events if isinstance(e.get(field), (int, float))
                  and not isinstance(e[field], bool) and math.isfinite(e[field]) and e[field] >= 0)


def aggregate(events):
    distribution = {status: sum(e["status"] == status for e in events) for status in TERMINAL}
    elapsed, prompts = numbers(events, "elapsed_s"), numbers(events, "prompt_bytes")
    percentile = lambda p: elapsed[max(0, math.ceil(len(elapsed) * p) - 1)] if elapsed else None
    outcomes = {c: sum(e.get("outcome_class") == c for e in events) for c in OUTCOME_CLASSES}
    failure_reasons, cancel_reasons = {}, {}
    for e in events:
        if e.get("status") == "failed":
            key = e.get("failure_reason") or "(none)"
            failure_reasons[key] = failure_reasons.get(key, 0) + 1
        if e.get("status") == "cancelled":
            key = e.get("cancel_reason") or "unspecified"
            cancel_reasons[key] = cancel_reasons.get(key, 0) + 1
    classes = ("files_narrow", "read_only_write", "pre_existing_main_failure",
               "upstream_transient", "soft_block", "bad_input", "other")
    return {"total": len(events), "success_rate": distribution["done"] / len(events) if events else None,
            "distribution": distribution,
            "elapsed_s": {"avg": sum(elapsed) / len(elapsed) if elapsed else None,
                          "p50": percentile(.5), "p95": percentile(.95), "samples": len(elapsed)},
            "mean_prompt_bytes": sum(prompts) / len(prompts) if prompts else None,
            "prompt_samples": len(prompts),
            "blocked_reason_classes": {c: sum(e.get("blocked_reason_class") == c for e in events)
                                       for c in classes},
            "narrow_reason_classes": {c: sum(e.get("narrow_reason_class") == c for e in events)
                                      for c in NARROW_CLASSES},
            "cancelled_after_done": sum(e.get("cancelled_after_done") is True for e in events),
            "outcome_classes": outcomes,
            # Excludes operator cancels, external signals and infra errors; None when nothing is left.
            "task_success_rate": (outcomes["success"] / (outcomes["success"] + outcomes["task"])
                                  if outcomes["success"] + outcomes["task"] else None),
            "failure_reasons": failure_reasons,
            "cancel_reasons": cancel_reasons}


def collect_stats(root, since="2h", project=None, pool=None, now=None):
    """Prefer rotated events; supplement missing IDs from done without writing history.

    Concurrent transitions/rotation are a best-effort snapshot. Ignore malformed
    lines (including an in-progress append); done records fill any resulting gap.
    """
    now = now or datetime.now(TZ)
    cutoff = parse_since(since, now)
    root = Path(root)
    events = {}
    paths = [p for p in (root / "logs").glob("events.jsonl.*") if p.suffix[1:].isdigit()]
    paths.sort(key=lambda p: int(p.suffix[1:]), reverse=True)
    paths.append(root / "logs/events.jsonl")
    for path in paths:
        try:
            with path.open(errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                        continue
                    at = timestamp(event.get("at"))
                    if event.get("status") in TERMINAL and at and cutoff <= at <= now:
                        events[event["id"]] = event
        except OSError:
            continue
    backfilled = 0
    for path in (root / "done").glob("*.json"):
        try:
            job = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(job, dict) or not isinstance(job.get("id"), str):
            continue
        if job["id"] in events:
            events[job["id"]] = classify_event(events[job["id"]], job)
            continue
        at = timestamp(job.get("finished_at"))
        if job.get("status") in TERMINAL and at and cutoff <= at <= now:
            events[job["id"]] = job_event(job)
            backfilled += 1
    events = {k: classify_event(e) for k, e in events.items()}
    selected = [e for e in events.values() if (project is None or e.get("project") == project)
                and (pool is None or e.get("pool") == pool)]
    return dict(schema_version="1", since=cutoff.isoformat(), until=now.isoformat(),
                filters={"project": project, "pool": pool}, **aggregate(selected),
                by_pool={p: aggregate([e for e in selected if e.get("pool") == p]) for p in POOLS},
                by_project={p: aggregate([e for e in selected if (e.get("project") or "(unknown)") == p])
                            for p in sorted(set(e.get("project") or "(unknown)" for e in selected))},
                sources={"events": len(events) - backfilled, "backfilled": backfilled})


def format_stats(result):
    def row(label, group):
        rate = "—" if group["success_rate"] is None else "%.1f%%" % (group["success_rate"] * 100)
        latency = group["elapsed_s"]
        seconds = lambda v: "—" if v is None else "%.1fs" % v
        prompt = group["mean_prompt_bytes"]
        return "%s: %s success (%s terminal); %s; elapsed avg/p50/p95 %s/%s/%s; mean prompt %s" % (
            label, rate, group["total"],
            " ".join("%s=%s" % item for item in group["distribution"].items()),
            seconds(latency["avg"]), seconds(latency["p50"]), seconds(latency["p95"]),
            "—" if prompt is None else "%.0f B" % prompt)
    def breakdown(group):
        rate = group["task_success_rate"]
        o = group["outcome_classes"]
        return ("Task success (excl. operator/interrupted/infra): %s of %d; "
                "task=%d infra=%d interrupted=%d operator=%d" % (
                    "—" if rate is None else "%.1f%%" % (rate * 100), o["success"] + o["task"],
                    o["task"], o["infra"], o["interrupted"], o["operator"]))
    reasons = lambda d: " ".join("%s=%s" % kv for kv in sorted(d.items(), key=lambda kv: -kv[1])) or "—"
    return "\n".join(["Window: %s → %s" % (result["since"], result["until"]), row("All", result),
                      breakdown(result),
                      "Failure reasons: " + reasons(result["failure_reasons"]),
                      "Files narrow: " + reasons(result["narrow_reason_classes"]),
                      "Cancel reasons: " + reasons(result["cancel_reasons"])] +
                     [row("Pool " + p, g) + "\n  " + breakdown(g) for p, g in result["by_pool"].items()] +
                     [row("Project " + p, g) + "\n  " + breakdown(g) for p, g in result["by_project"].items()])
