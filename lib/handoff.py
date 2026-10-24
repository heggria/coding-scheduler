"""Deterministic, evidence-first handoff cards; never certify acceptance."""
import json
import re
import shlex
from pathlib import Path


STATUSES = ("pass", "fail", "not_run", "unknown")
RECORD = re.compile(r"^VERIFICATION:\s*(\{.*\})\s*$")


def compact(value, limit=160):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def verification(item, source):
    """A worker exit is deliberately never passed to this function."""
    status = item.get("status", "unknown")
    code = item.get("exit")
    if type(code) is not int:
        code = None
    if code is not None:
        status = "pass" if code == 0 else "fail"
    return {"status": status if status in STATUSES else "unknown",
            "command": item.get("command") if isinstance(item.get("command"), str) else None,
            "exit": code,
            "log": item.get("log") if isinstance(item.get("log"), str) else None,
            "source": source}


def extract(text, path, records_only=False):
    checks, risks, summary = [], [], None
    section = ""
    fenced = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        source = "%s#L%d" % (path, number) if path else "conclusion#L%d" % number
        if not records_only:
            if line.startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
        record = RECORD.fullmatch(line)
        if record:
            try:
                item = json.loads(record[1])
                checks.append(verification(item, source))
            except (ValueError, TypeError):
                checks.append(verification({}, source))
            continue
        if records_only:
            continue
        if not line:
            continue
        plain = re.sub(r"^[#*\-\s]+", "", line).strip()
        if re.match(r"(?:STATUS|NEED_FILES|OUT_OF_SCOPE_FILES):", plain):
            continue
        heading = plain.rstrip(":：").lower()
        if line.startswith("#") or ("`" not in line and line.endswith((":", "："))):
            section = heading
            continue
        is_risk = bool(re.search(r"risk|unverified|limitations|pre-existing failures|风险|未验证|遗留", section))
        is_next = bool(re.search(r"next|suggest|下一步|建议", section + " " + plain))
        if is_risk or re.match(r"风险|未验证|遗留|Risk|Unverified", plain, re.I):
            risks.append(compact(plain))
            continue
        if is_next:
            continue
        commands = list(re.finditer(r"`([^`]+)`", line))
        is_check = bool(re.search(r"verif|test|check|验证|测试", section + " " + plain, re.I))
        if commands and is_check:
            # Multiple commands on one prose line have ambiguous result ownership.
            # shortcut: only inline commands carry results; fenced/ambiguous output stays unknown.
            result = re.sub(r"`[^`]+`", "", plain)
            status = "unknown"
            if len(commands) == 1:
                if re.search(r"not[_ -]run|not tested|未(?:运行|执行|测试|验证)|没(?:运行|执行)|未跑|未测|跳过|skipped", result, re.I):
                    status = "not_run"
                elif re.search(r"\bfail(?:ed|ure|ures)?\b|\berrors?\b|not (?:yet )?pass(?:ed)?|(?:未|不|没有|未能)通过|失败", result, re.I):
                    status = "fail"
                elif re.search(r"should|expect|will|must|计划|待运行|待执行|待验证|预计|预期|需要|应当", result, re.I):
                    status = "unknown"
                elif re.search(r"\bpass(?:ed)?\b|\bsuccess(?:ful)?\b|通过|成功|全绿", result, re.I):
                    status = "pass"
            code = re.search(r"\bexit(?:_code)?\s*[=:]\s*(-?\d+)\b", result, re.I)
            for command in commands:
                item = {"command": command[1], "status": status}
                if code and len(commands) == 1:
                    item["exit"] = int(code[1])
                checks.append(verification(item, source))
            continue
        if summary is None and not commands and not plain.startswith(("http://", "https://")):
            if not re.search(r"verif|test|check|验证|测试", section, re.I) and not re.search(r"commit|产物|artifact|PR[:：]|SHA", plain, re.I):
                summary = compact(plain)
    return summary, checks, risks


def build_handoff(job, summary, conclusion=""):
    artifacts = dict(summary.get("artifacts") or {})
    short, checks, risks = extract(conclusion, artifacts.get("conclusion"))
    supplied = job.get("verification")
    if isinstance(supplied, list):
        checks = [verification(item, "job.verification") for item in supplied if isinstance(item, dict)] + checks
    # Only explicit evidence records in the bounded log tail count, never raw
    # "passed" text inside model transcripts, prompts or worker exit messages.
    log = artifacts.get("log")
    if log:
        try:
            with Path(log).open("rb") as handle:
                size = handle.seek(0, 2)
                handle.seek(max(0, size - 65536))
                tail = handle.read(65536).decode("utf-8", errors="replace")
            if size > 65536:
                tail = tail.partition("\n")[2]
            _, logged, _ = extract(tail, None, records_only=True)
            for item in logged:
                item["source"] = str(log) + " (tail)"
                item["log"] = item["log"] or str(log)
            checks.extend(logged)
        except (OSError, TypeError, ValueError):
            pass
    checks = list({json.dumps(item, sort_keys=True): item for item in checks}.values())
    if not checks:
        checks = [verification({}, "no_verification_evidence")]
    risks.append("验收未证明：验证项仅为记录的证据，未核对全部 AC；调度 done / worker exit=0 不代表 AC 通过。")
    if any(item["status"] != "pass" for item in checks):
        risks.append("存在 fail / not_run / unknown 验证项，需人工核对或补跑。")
    risks.append("changed_files / diff_stat 可能包含原有脏文件或同 cwd 其他任务改动，不能独占归因。")
    for field in ("failure_reason", "failure_detail", "reason_text"):
        if job.get(field):
            risks.append(compact(job[field]))
    if isinstance(summary.get("warnings", job.get("warnings")), list):
        risks.extend(compact(warning) for warning in summary.get("warnings", job.get("warnings", [])))
    next_steps = ["审阅 conclusion 和验证来源，对照 AC 补跑缺失检查；以下建议仅供复制，不自动执行。"]
    if artifacts.get("conclusion"):
        next_steps.append("cat -- " + shlex.quote(str(artifacts["conclusion"])))
    if log:
        next_steps.append("tail -n 80 -- " + shlex.quote(str(log)))
    if job.get("cwd") and job.get("base_sha"):
        next_steps.append("git -C %s diff %s -- ." % (shlex.quote(str(job["cwd"])), shlex.quote(str(job["base_sha"]))))
    return {"summary": short or "unknown（缺少可提取的改动摘要）",
            "freshness": summary.get("freshness", "unknown"),
            "scheduler_status": summary.get("status", "unknown"), "worker_exit": summary.get("exit"),
            "acceptance": {"status": "unproven", "label": "验收未证明"},
            "artifacts": artifacts, "pr_links": summary.get("pr_links", []),
            "verification": checks, "attribution": "shared_cwd_or_unknown",
            "changed": {"files": summary.get("changed_files"), "diff_stat": summary.get("diff_stat"),
                        "attribution": "shared_cwd_or_unknown"},
            "risks": list(dict.fromkeys(risks)), "next_steps": next_steps}


def render_handoff(card, job_id):
    """Keep display bounded; the JSON lists and evidence paths remain complete."""
    lines = ["接手卡 %s | 调度=%s worker_exit=%s | 验收未证明" %
             (compact(job_id, 80), card["scheduler_status"], card["worker_exit"]),
             "freshness: " + card.get("freshness", "unknown"),
             "做了什么: " + card["summary"],
             "产物: " + compact("; ".join("%s=%s" % (key, value) for key, value in card["artifacts"].items() if value), 500)]
    if card["pr_links"]:
        lines.append("PR: " + compact("; ".join(card["pr_links"]), 300))
    files = card["changed"]["files"]
    lines.append("changed (%s): %s" % (card["attribution"], compact(
        ", ".join(files) if files else ("无记录改动（非独占证据）" if files == [] else "unknown"), 240)))
    lines.append("验证（命令结果，不等于全部 AC）:")
    for item in card["verification"][:4]:
        lines.append("  [%s] %s | exit=%s | %s" %
                     (item["status"], compact(item["command"] or "unknown", 120), item["exit"],
                      compact(item["log"] or item["source"], 240)))
    if len(card["verification"]) > 4:
        lines.append("  …另 %d 项，详见 --json handoff.verification" % (len(card["verification"]) - 4))
    lines.append("风险/未验证: " + compact("; ".join(card["risks"]), 400))
    lines.append("下一步（仅建议）:")
    # Copyable shell suggestions must retain their quoting and full paths.
    lines.extend("  " + step for step in card["next_steps"][:4])
    return "\n".join(lines)
