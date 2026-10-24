"""Explicit local delivery of scheduler terminal events; no worker side effects."""
import hashlib
import json
import re
import shlex
from datetime import datetime, timezone

import core
from stats import TERMINAL


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def notification(root, event, key):
    jid = event["id"]
    job = core.find_job(root, jid) or dict(event)
    job = dict(job, status=event["status"], exit=event.get("exit"))
    summary = core.job_summary(job, lines=0)
    card = summary["handoff"]
    inspect = "cs status %s --no-reclaim" % shlex.quote(jid)
    try:
        core.resume_session(job, job.get("area"))
        next_step = ("可用 cs submit --resume-of %s 并补齐新任务参数；"
                     "仍须原 cwd、匹配 pool 和现有 submit 校验，不自动续聊。" % shlex.quote(jid))
    except ValueError as exc:
        next_step = str(exc)
    text = ("[%s] job=%s | 接手卡: %s\n摘要: %s\n结论: %s\n"
            "验收未证明；下一步: %s；%s" %
            (event["status"], jid, inspect, card["summary"],
             card["artifacts"].get("conclusion") or "unknown", inspect, next_step))
    return {"key": key, "id": jid, "status": event["status"], "at": event.get("at"),
            "handoff": {"command": inspect, "summary": card["summary"],
                        "artifacts": card["artifacts"], "acceptance": card["acceptance"]},
            "next_step": next_step, "text": text}


def drain(root, channel="off", output=print):
    """Caller holds core.state_lock. Rescan retained logs to survive rotation.

    The cursor is an observation watermark, never a delivery acknowledgement:
    failed deliveries and out-of-order/reclassified events must remain retryable.
    """
    if channel not in ("off", "terminal", "inbox"):
        raise ValueError("CS_NOTIFY_CHANNEL must be off, terminal or inbox")
    result = {"enabled": channel != "off", "channel": channel, "delivered": [], "errors": []}
    if channel == "off":
        return result
    directory = root / "notify"
    directory.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / "deliveries.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    if (not isinstance(ledger, dict) or any(
            not isinstance(record, dict) or type(record.get("attempts")) is not int or
            type(record.get("delivered")) is not bool for record in ledger.values())):
        raise ValueError("invalid notify/deliveries.json")
    paths = [p for p in (root / "logs").glob("events.jsonl.*") if p.suffix[1:].isdigit()]
    paths.sort(key=lambda p: int(p.suffix[1:]), reverse=True)
    paths.append(root / "logs/events.jsonl")
    events, cursor = {}, {"files": {}, "scanned_at": datetime.now(timezone.utc).isoformat()}
    for path in paths:
        if not path.exists():
            continue
        with path.open() as source:
            for line in source:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if (not isinstance(event, dict) or event.get("status") not in TERMINAL or
                        not isinstance(event.get("id"), str) or
                        not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", event["id"])):
                    continue
                identity = json.dumps([event["id"], event["status"], event.get("at")], ensure_ascii=False)
                key = hashlib.sha256(identity.encode()).hexdigest()
                events[key] = event
                cursor["last_event"] = key
            cursor["files"][path.name] = source.tell()
    save(directory / "cursor.json", cursor)
    priority = {"blocked": 0, "unknown": 0, "failed": 1, "cancelled": 2, "done": 3}
    for key, event in sorted(events.items(), key=lambda item: priority[item[1]["status"]]):
        previous = ledger.get(key, {})
        if previous.get("delivered"):
            continue
        record = dict(id=event["id"], status=event["status"], at=event.get("at"),
                      attempts=previous.get("attempts", 0) + 1, channel=channel, delivered=False)
        try:
            message = notification(root, event, key)
            if channel == "inbox":
                inbox = directory / "inbox"
                inbox.mkdir(exist_ok=True)
                # Deterministic names also prevent duplicates after a crash before ledger save.
                save(inbox / (key + ".json"), message)
            else:
                output(message["text"])
            record["delivered"] = True
            result["delivered"].append({"id": event["id"], "status": event["status"], "key": key})
        except (OSError, ValueError) as exc:
            record["error"] = str(exc)
            result["errors"].append("notify %s: %s" % (event["id"], exc))
        ledger[key] = record
        save(ledger_path, ledger)
    return result
