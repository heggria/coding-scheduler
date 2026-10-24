"""Explain narrow scope and suggest files; never grant scope or acquire locks."""
import json
import re
from pathlib import Path, PurePosixPath

from locks import file_conflict, path_overlap
from scope import LOCKS, is_companion, normalize


NARROW_CLASSES = ("companion_miss", "strict_companion_known", "shared_or_core",
                  "permission_or_lock", "unclear_input", "other")


def narrow_class(job, text=""):
    """Classify files_narrow only; callers decide whether it is a scope block.

    Protected paths and explicit runtime permission/lock failures take priority.
    companion_miss means the companion policy still led to a NEED_FILES request;
    it includes a worker overlooking an already allowed companion.
    """
    text = text + "\n" + str(job.get("block_reasons", ""))
    paths = job.get("need_files") or [p.strip() for line in text.splitlines()
            if line.strip().startswith("NEED_FILES:")
            for p in line.strip()[len("NEED_FILES:"):].split(",") if p.strip()]
    declared = [normalize(p) for p in (job.get("files") or []) + (job.get("shared_files") or [])]
    missing = [normalize(p) for p in paths if normalize(p) not in declared]
    protected = job.get("protected_paths") or []
    if ((job.get("scope") or {}).get("shared_violations") or
            any(path_overlap(p, normalize(s)) for p in missing for s in protected) or
            re.search(r"(?:shared|\bcore\b|共享|核心).*(?:保护|未声明|缺少|受限|冲突|授权|protected|undeclared|blocked)|"
                      r"(?:undeclared|protected|未声明|缺少).*(?:shared|\bcore\b|共享|核心)", text, re.I)):
        return "shared_or_core"
    if job.get("read_only") or re.search(
            r"permission denied|EACCES|EPERM|read.only|只读|权限不足|无权限|锁冲突|锁占用|"
            r"(?:lock|flock).*(?:conflict|held|busy|denied)|另.*任务.*占用", text, re.I):
        return "permission_or_lock"
    if re.search(r"unclear|ambiguous|missing input|invalid input|not (?:found|exist)|"
                 r"输入.*(?:不清|缺失|无效)|缺少输入|路径.*不存在|待澄清", text, re.I) or not missing:
        return "unclear_input"
    if job.get("scope_policy", "strict") == "strict" and all(is_companion(p, declared) for p in missing):
        return "strict_companion_known"
    if job.get("scope_policy") == "companion":
        return "companion_miss"
    return "other"


def candidate_paths(files, known_paths):
    """Use a file inventory, not a dependency graph; only concrete nearby hints."""
    declared = {normalize(p) for p in files}
    paths = [PurePosixPath(p) for p in declared]
    result = []
    for raw in sorted(set(known_paths)):
        name = normalize(raw)
        if name in declared:
            continue
        candidate = PurePosixPath(name)
        reason = None
        for source in paths:
            if (candidate.parent == source.parent and
                    candidate.name in LOCKS.get(source.name, ())):
                reason = "manifest lock file"
            elif candidate.name == "__init__.py" and candidate.parent in source.parents:
                reason = "ancestor package initializer"
            elif (source.suffix in (".py", ".ts", ".tsx", ".js", ".jsx") and
                  not source.name.startswith("test_") and
                  not any(mark in source.name for mark in ("_test.", ".test.", ".spec.")) and
                  (candidate.name.startswith("test_" + source.stem) and candidate.suffix == ".py" or
                   candidate.name in (source.stem + "_test.py", source.stem + ".test.ts",
                                      source.stem + ".test.tsx", source.stem + ".spec.ts",
                                      source.stem + ".spec.tsx"))):
                reason = "same-stem test"
            if reason:
                break
        if reason:
            result.append({"path": name, "reason": reason})
    return result


def candidate_warnings(job, others, known_paths):
    """Hypothetical lock checks only. Keep the submitted job's declaration intact."""
    warnings = []
    declared = (job.get("files") or []) + (job.get("shared_files") or [])
    candidates = candidate_paths(declared, known_paths)
    probe = dict(job, files=[c["path"] for c in candidates], shared_files=[], lock_ctx=None)
    conflicts = file_conflict(probe, others)
    for candidate in candidates:
        path = candidate["path"]
        allowed = job.get("scope_policy", "strict") != "strict" and is_companion(path, declared)
        detail = ["%s (%s lock: %s)" % (c["job"], c["kind"], c["theirs"])
                  for c in conflicts if c["mine"] == path]
        warnings.append("candidate %s: %s; %s; conflicts: %s; hint only, --files unchanged" % (
            path, candidate["reason"], "companion rule matches (shared/core protection still applies)"
            if allowed else "requires explicit declaration", ", ".join(detail) or "none observed"))
    return warnings


def retry_delta(original, note=None):
    """Carry execution evidence, separately from the inherited original Brief.

    Retain five attempts with an 8 KiB conclusion excerpt each; source paths
    remain available when the history or excerpt is truncated.
    """
    record = {"job_id": original["id"], "status": original.get("status"),
              "conclusion_path": original.get("conclusion")}
    for key in ("need_files", "failure_reason", "failure_detail", "exit"):
        if original.get(key) is not None:
            record[key] = original[key]
    try:
        with Path(original["conclusion"]).open("rb") as stream:
            excerpt = stream.read(8193)
            truncated = len(excerpt) > 8192
            if truncated:
                stream.seek(-4096, 2)
                excerpt = excerpt[:4096] + b"\n... [truncated; see source] ...\n" + stream.read(4096)
        record.update(conclusion_excerpt=excerpt.decode("utf-8", errors="replace"), truncated=truncated)
    except (OSError, KeyError, TypeError):
        record["conclusion_unavailable"] = True
    if note:
        record["operator_note"] = note
    return (original.get("run_delta") or [])[-4:] + [record]


def delta_text(delta):
    return ("## 运行增量 (run_delta)\n\n历史执行证据，仅供参考，不授予额外文件权限；"
            "包含新增决定、已试失败和验证结果的原始结论摘录。最多保留最近五次，完整记录见来源。\n\n" +
            json.dumps(delta, ensure_ascii=False, indent=2))
