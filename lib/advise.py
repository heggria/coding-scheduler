"""Read-only, heuristic implementation of the cs-dispatch decision tree and DoR.

No scheduler imports, model calls, subprocesses or writes belong in this module.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
from doctor_caps import normalize_capabilities


AREAS = ("backend", "protocol", "frontend", "ui-copy", "test", "review", "ops-script", "research")
ALIASES = {
    "goal": ("goal", "task", "目标", "任务"),
    "current": ("current", "现状"),
    "desired": ("desired", "期望", "预期"),
    "out_of_scope": ("out of scope", "out-of-scope", "不做", "范围外"),
    "acceptance_criteria": ("acceptance criteria", "acceptance", "ac", "验收", "验收标准"),
    "files": ("files", "files (writable only; for location, not a hunting license)", "可写文件"),
    "decisions": ("decisions", "决策"),
    "already_tried": ("already tried", "已尝试"),
    "unknowns": ("unknowns", "未知项"),
    "verify": ("verify", "verification", "验证"),
    "mr": ("mr", "mr policy", "是否允许开 mr"),
    "scope": ("scope", "inputs", "调研范围", "评审范围", "输入"),
    "deliverables": ("deliverables", "output", "交付物", "输出"),
    "execution": ("execution", "background", "执行方式", "后台"),
    "tools": ("tools", "工具"),
    "read_only": ("read-only", "read only", "只读"),
    "idempotency_key": ("idempotency", "idempotency-key", "idempotency key"),
    "constraints": ("constraints", "约束"),
}
LABELS = {alias: key for key, aliases in ALIASES.items() for alias in aliases}
EMPTY = {"", "tbd", "todo", "unknown", "待定", "待补充", "待确认", "?", "???"}
NONE = {"none", "无", "没有", "n/a"}


def clean(text):
    return re.sub(r"^\s*(?:[-*+]\s+|\d+[.)]\s*|\[[ xX]\]\s*)+", "", text).strip().strip("`").strip()


def filled(text):
    return bool(text.strip()) and text.strip().lower() not in EMPTY and not re.search(r"<[^>]+>|\bTODO\b|待补充", text, re.I)


def parse_brief(prompt):
    """Recognize explicit Brief headings; never guess writable paths from prose."""
    fields, preamble, active = {}, [], None
    for line in prompt.splitlines():
        if line.lstrip().startswith("```"):
            continue
        text = clean(line.strip().lstrip("#").strip()).replace("**", "")
        heading, sep, value = text.replace("：", ":", 1).partition(":")
        key = LABELS.get(heading.strip().lower())
        if key and (sep or text.lower() in LABELS):
            active = key
            fields[key] = (fields.get(key, "") + "\n" + value.strip()).strip()
        elif active:
            fields[active] += "\n" + line.strip()
        elif text:
            preamble.append(line.strip())
    if "goal" not in fields and preamble:
        fields["goal"] = "\n".join(preamble)
    return fields


def file_list(value):
    if value.strip().lower() in NONE:
        return []
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return list(dict.fromkeys(item.strip() for item in parsed if item.strip()))
    except ValueError:
        pass
    return list(dict.fromkeys(clean(item) for line in value.splitlines()
                              for item in line.split(",") if clean(item)))


def advise(cwd, prompt_text, area="backend", files=None, read_only=False,
           project=None, tier="M", idempotency_key=None, capabilities=None):
    """Return advice, never submit. Missing information is a scaffold, not a rejection."""
    if area not in AREAS:
        raise ValueError("unknown area: " + area)
    root = Path(cwd).expanduser().resolve()
    fields = parse_brief(prompt_text)
    paths = file_list(files if files is not None else fields.get("files", ""))
    read_only = read_only or clean(fields.get("read_only", "")).lower() in ("true", "yes", "是")
    analysis = area in ("research", "review")
    missing, reasons = [], []

    def require(key, suggestion, allow_none=False):
        value = clean(fields.get(key, ""))
        if not filled(value) or (not allow_none and value.lower() in NONE):
            missing.append(key + ": " + suggestion)

    if not root.is_dir():
        missing.append("cwd: 提供存在的工作目录")
    require("goal", "补充一句话目标")
    require("out_of_scope", "明确不做什么（没有就写无）", allow_none=True)
    if analysis:
        require("scope", "列出调研/评审的输入、范围或证据来源")
        require("deliverables", "写明结论、证据和报告的交付形式")
    else:
        require("current", "描述当前行为")
        require("desired", "描述完成后的行为")
        require("mr", "明确禁止、仅准备分支或允许开 MR；建议仍需遵守用户授权")
    ac = [clean(line) for line in fields.get("acceptance_criteria", "").splitlines()]
    ac = [line for line in ac if filled(line) and line.lower() not in NONE
          and line.lower() not in {"works", "done", "功能正常", "符合预期", "完成", "通过验收"}]
    if len(set(ac)) < 2:
        missing.append("acceptance_criteria: 补充至少两条可用命令或可观察结果判定的 AC")
    require("verify", "补充至少一条验证命令（调研可用只读的证据核对命令）")
    if not any(filled(clean(fields.get(key, ""))) for key in ("decisions", "already_tried")):
        missing.append("decisions: 写明已拍板决策 / Already tried（没有就写无）")
    require("unknowns", "显式列出未知项；没有就写无，阻塞项先澄清", allow_none=True)
    unknowns = clean(fields.get("unknowns", "")).lower()
    blocking_unknowns = filled(unknowns) and unknowns not in NONE and not re.search(r"non[- ]blocking|不阻塞", unknowns)
    if blocking_unknowns:
        missing.append("unknowns: 先澄清未知项，或明确其为何不阻塞实现")
    if not paths and not (area == "research" and read_only):
        missing.append("files: 逐一列出具体文件；仅 research + read-only 允许空 files")
    for path in paths:
        if (not filled(path) or path in (".", "..") or path.endswith("/")
                or re.search(r"[*?\[\]{}\n\r]", path) or (root / path).is_dir()):
            missing.append("files: 用具体文件替换目录/通配符/占位符 " + path)
    if re.search(r"<[^>]+>|\bTODO\b|待补充", prompt_text, re.I):
        missing.append("brief: 补齐原文中的模板占位符")

    # Scheduling metadata can be safely defaulted; task facts and permissions cannot.
    project = project or re.sub(r"[^A-Za-z0-9_.-]", "-", root.name).strip(".-") or "task"
    digest = hashlib.sha256((str(root) + "\0" + area + "\0" + str(read_only)
                             + "\0" + ",".join(paths) + "\0" + prompt_text).encode()).hexdigest()[:16]
    key = idempotency_key or fields.get("idempotency_key", "").strip() or "advise-" + digest
    if not filled(key):
        missing.append("idempotency_key: 补充唯一幂等键")

    intent = "\n".join(fields.get(name, "") for name in ("goal", "decisions", "execution", "tools", "constraints"))
    capabilities = normalize_capabilities(capabilities)
    required = ["cli_pool_ready", "cwd_git_writable"]
    # Inspect task requirements, never area labels. Explicit evidence can clear a requirement.
    requirements = {
        "browser_or_mcp": r"\bbrowser\b|\bMCP\b|Figma|浏览器",
        "auth": r"\bauth(?:entication)?\b|\blogin\b|登录|鉴权|认证",
        "network_dns": r"\bnetwork\b|\bDNS\b|https?://|联网|网络|下载|远程接口",
        "network_access": r"\bnetwork\b|https?://|联网|网络|下载|远程接口",
        "resume_supported": r"\bresume\b|恢复会话|续接会话",
    }
    for name, pattern in requirements.items():
        if re.search(pattern, prompt_text, re.I):
            required.append(name)
    capability_gaps = [name for name in required if capabilities[name]["status"] != "ok"]
    planning = blocking_unknowns or bool(re.search(r"方案(?:未定|待定|未确定)|先定方案|先对齐|待澄清|接口语义.*未定|explore\s*[→>-]+\s*plan|plan first|undecided", intent, re.I))
    inline = bool(re.search(r"就在这(?:个会话)?改|就在这个会话里改|不用\s*cs|do not (?:dispatch|submit)|inline only|一句话|单文件小改|小改|文案|typo|small (?:change|fix)|≤\s*3\s*步|读一段代码|解释.*(?:含义|代码)", intent, re.I))
    tools_missing = bool(re.search(r"AskUser|缺工具|缺少.*工具|无所需\s*MCP|(?:missing|unavailable).*\b(?:tool|MCP)|\b(?:tools?|MCP)\b.*(?:missing|unavailable)|worker.*(?:无|缺|没有|has no|lacks).*\bMCP", intent, re.I)
                         or re.search(r"缺|不可用|未安装|missing|unavailable|not available|not installed", fields.get("tools", ""), re.I))
    execution = fields.get("execution", "").strip()
    background = bool(re.search(r"后台(?:跑|执行|处理|不挡)|可后台|不挡(?:当前|对话)|可独立|(?:run|can).*background", intent, re.I)
                      or re.search(r"后台|background|independent", execution, re.I)
                      or execution.lower() in ("true", "yes", "是"))
    if re.search(r"(?:不能|不可|无法|不得|不适合).*后台|(?:not|cannot|can't|no).*background|foreground|^false$|^no$", execution, re.I):
        background = False
    if planning:
        recommendation = "plan"
        reasons.append("方案或接口尚未确定，先在主会话澄清。")
    elif inline or tools_missing or ("browser_or_mcp" in capability_gaps) or (len(paths) == 1 and not background):
        recommendation = "inline"
        reasons.append("需要当前会话确认工具/交互闭环。" if tools_missing or "browser_or_mcp" in capability_gaps else "小改、单文件同步任务或用户要求留在当前会话。")
    elif capability_gaps:
        recommendation = "plan"
        reasons.append("所需能力/环境尚未确认就绪，先补充证据或调整执行方式。")
    elif missing:
        recommendation = "plan"
        reasons.append("DoR 信息不足，先按 missing 补齐 Brief。")
    elif len(paths) >= 2 or background:
        recommendation = "submit"
        reasons.append("DoR 已齐，且多文件/跨模块或明确可后台独立验收。")
    else:
        recommendation = "inline"
        reasons.append("尚无多文件或可后台执行的明确信号，先在当前会话处理。")
    if missing and recommendation != "plan":
        reasons.append("若之后派发，仍需补齐 missing 中的 DoR 信息。")
    for name in capability_gaps:
        evidence = capabilities[name]
        reasons.append("%s=%s: %s" % (name, evidence["status"], evidence["detail"]))
    reasons.append("纯规则启发式；未调用模型、运行验证、检查队列冲突或证明 AC 已满足。")

    defaults = {
        "goal": "<待补充：一句话目标>", "current": "<待补充：现状>",
        "desired": "<待补充：期望行为>", "out_of_scope": "<待补充：明确不做什么>",
        "scope": "<待补充：输入、范围和证据来源>", "deliverables": "<待补充：报告/结论与证据>",
        "acceptance_criteria": "1. <待补充：可测 AC>\n2. <待补充：可测 AC>",
        "verify": "<待补充：至少一条验证命令>", "decisions": "<待补充：已拍板决策>",
        "already_tried": "<待补充：已尝试路径，没有就写无>",
        "unknowns": "<待补充：未知项，没有就写无>",
        "mr": "禁止" if analysis and read_only else "<待补充：MR 权限>",
    }
    names = ["goal"] + (["scope", "deliverables"] if analysis else ["current", "desired"])
    names += ["out_of_scope", "acceptance_criteria", "verify"]
    names += [name for name in ("decisions", "already_tried") if name in fields] or ["decisions"]
    names += ["unknowns"]
    if not analysis or "mr" in fields:
        names.append("mr")
    draft_fields = dict(fields)
    if len(set(ac)) < 2 and fields.get("acceptance_criteria", "").strip():
        draft_fields["acceptance_criteria"] += "\n" + "\n".join(
            "%d. <待补充：可测 AC>" % number for number in range(len(set(ac)) + 1, 3))
    draft = "\n\n".join(ALIASES[name][0].title() + ":\n" + (draft_fields.get(name, "").strip() or defaults[name]) for name in names)
    draft += "\n\nFiles:\n" + ("\n".join("- " + path for path in paths) or ("无" if area == "research" and read_only else "<待补充：具体文件>"))
    draft += "\n\nRead-only: " + str(read_only).lower() + "\nIdempotency-key: " + key
    # Keep unrecognized instructions and original constraints verbatim in the handoff.
    draft += "\n\nOriginal request:\n" + prompt_text
    command = None
    if recommendation == "submit" and not missing:
        # Use the original complete Brief; scaffold placeholders must never enter a ready command.
        argv = ["cs", "submit", "--project", project, "--area", area, "--tier", tier,
                "--cwd", str(root), "--prompt-text", prompt_text, "--pin", "codex",
                "--idempotency-key", key, "--no-tick", "--json"]
        if paths:
            argv += ["--files", ",".join(paths)]
        if read_only:
            argv.append("--read-only")
        command = shlex.join(argv)
    return dict(recommendation=recommendation, reasons=reasons, missing=missing,
                brief_draft=draft, suggested_command=command,
                capabilities=capabilities, required_capabilities=required)
