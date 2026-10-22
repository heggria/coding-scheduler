"""Bounded, read-only execution summaries; never interpret prompts as activity."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 1024 * 1024
TEST = re.compile(r"Ran \d+ tests?|^(?:OK|FAILED)\b|\d+ (?:passed|failed)\b|Tests?:\s|[✓✗].*(?:tests?|passed|failed)")
SECRET = re.compile(r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|Bearer\s+\S+|(?:token|password|secret)=\S+", re.I)
READ_TOOLS = {"read", "grep", "glob", "Read", "Grep", "Glob", "list"}


def redact(text):
    return SECRET.sub("***", str(text))


def read_tail(path):
    try:
        with Path(path).open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - MAX_BYTES))
            data = stream.read(MAX_BYTES)
        if size > MAX_BYTES:
            data = data.partition(b"\n")[2]
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def safe_file(directory, name):
    target = directory / name
    try:
        return target if target.resolve().parent == directory.resolve() and target.is_file() else None
    except (OSError, RuntimeError):
        return None


def moment(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except ValueError:
        return None


def emit(events, kind, text, ts=None, **extra):
    event = dict(kind=kind, text=redact(text), **extra)
    if ts is not None:
        event["ts"] = ts
    if "files" in event:
        event["files"] = [redact(path) for path in event["files"]]
    if kind == "error" and events and events[-1] == event:
        return
    events.append(event)


def output_events(events, output, ts=None):
    for line in str(output).splitlines():
        line = line.strip()
        if TEST.search(line):
            emit(events, "test", redact(line)[:200], ts)
        elif re.match(r"warning:|ERROR\b", line, re.I):
            emit(events, "error", redact(line)[:200], ts)


def command(events, cmd, output, code=None, duration=None, ts=None):
    text = redact(cmd)[:200]
    if duration:
        text += " · " + str(duration)
    if code not in (None, 0):
        text += "\n" + "\n".join(redact(line)[:200] for line in str(output).splitlines()[-3:])
    extra = {"exit": code} if code is not None else {}
    emit(events, "cmd", text, ts, **extra)
    output_events(events, output, ts)


def codex_events(text):
    events, mode, block = [], None, []

    def flush():
        if mode == "codex":
            emit(events, "say", redact("\n".join(block).strip())[:600])
        elif mode == "exec" and block:
            result = next((i for i, line in enumerate(block) if re.search(
                r"(?:succeeded|exited \d+) in [\d.]+(?:ms|s):\s*$", line)), None)
            if result is None:
                command(events, block[0], "")
            else:
                match = re.search(r"(succeeded|exited (\d+)) in ([\d.]+(?:ms|s)):", block[result])
                # Codex often prints command and execution result on the same line.
                cmd = "\n".join(block[:result] + [block[result][:match.start()].rstrip()])
                command(events, cmd, "\n".join(block[result + 1:]),
                        int(match.group(2) or 0), match.group(3))
        elif mode == "apply patch":
            files = []
            for line in block:
                if line.startswith("diff --git"):
                    break
                match = re.match(r"(?:[AMD] )?(/\S.*)", line)
                if match:
                    files.append(match.group(1))
                if line.startswith("patch: failed"):
                    emit(events, "error", line)
            if files:
                emit(events, "edit", "\n".join(files), files=files)
        elif mode == "tokens used" and block:
            emit(events, "end", "tokens used: " + block[0].strip())

    for line in text.splitlines():
        if line in ("user", "codex", "exec", "apply patch", "tokens used"):
            flush()
            mode, block = line, []
        elif mode != "user" and re.fullmatch(r"\*\*.+\*\*", line):
            flush()
            mode, block = None, []
            emit(events, "think", redact(line)[:120])
        elif mode not in ("user", "exec", "apply patch") and re.match(r"warning:|ERROR\b", line, re.I):
            emit(events, "error", redact(line)[:200])
        else:
            block.append(line)
    flush()
    return events


def tool_event(events, name, inputs, output, code=None, ts=None, error=None):
    if name in READ_TOOLS:
        return
    if name.lower() in ("shell", "bash", "exec_command"):
        command(events, inputs.get("command", inputs.get("cmd", "")), output, code, ts=ts)
    elif name.lower() in ("write", "edit", "apply_patch", "multiedit"):
        path = inputs.get("path", inputs.get("file_path"))
        files = [path] if isinstance(path, str) else []
        emit(events, "edit", "\n".join(files) or name, ts, files=files)
    elif name.lower() == "websearch":
        emit(events, "say", redact(inputs.get("query", ""))[:600], ts)
    if error:
        emit(events, "error", redact(error)[:600], ts)


def json_events(text, pool):
    events, tools, seen = [], {}, set()
    lines = text.splitlines()
    if pool == "opencode":
        latest = {}
        for index, line in enumerate(lines):
            try:
                row = json.loads(line)
                part = row.get("part") if isinstance(row, dict) else None
                if isinstance(part, dict) and part.get("id"):
                    key = (row.get("type"), part["id"])
                    if key in latest:
                        lines[latest[key]] = ""
                    latest[key] = index
            except ValueError:
                pass
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            if pool == "opencode":
                output_events(events, line)
            continue
        if not isinstance(row, dict):
            continue
        ts, kind = row.get("timestamp"), row.get("type")
        if pool == "opencode":
            part = row.get("part") or {}
            # Part IDs identify repeated snapshots; text equality alone does not.
            key = (kind, part.get("id"), json.dumps(part, sort_keys=True))
            if key in seen:
                continue
            seen.add(key)
            if kind == "text":
                emit(events, "say", redact(part.get("text", ""))[:600], ts)
            elif kind == "tool_use":
                state = part.get("state") or {}
                if state.get("status") not in ("running", "completed", "error"):
                    continue
                metadata = state.get("metadata") or {}
                code = (metadata.get("metadata") or {}).get("exit", metadata.get("exit"))
                tool_event(events, part.get("tool", ""), state.get("input") or {},
                           state.get("output", ""), code, ts, state.get("error"))
            elif kind == "step_finish":
                emit(events, "end", "tokens: %s · cost: %s · %s" % (
                    part.get("tokens", {}), part.get("cost", 0), part.get("reason", "")), ts)
            elif kind == "error":
                emit(events, "error", redact(json.dumps(row.get("error", {})))[:600], ts)
        elif kind in ("assistant", "user"):
            content = (row.get("message") or {}).get("content", [])
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                block_kind = block.get("type")
                if kind == "assistant" and block_kind in ("text", "thinking"):
                    value = block.get("text" if block_kind == "text" else "thinking", "")
                    if value.strip():
                        emit(events, "say" if block_kind == "text" else "think",
                             redact(value)[:600] if block_kind == "text" else redact(value.splitlines()[0])[:120], ts)
                elif kind == "assistant" and block_kind == "tool_use":
                    tools[block.get("id")] = (block.get("name", ""), block.get("input") or {}, ts)
                elif kind == "user" and block_kind == "tool_result":
                    tool = tools.pop(block.get("tool_use_id"), None)
                    if tool:
                        output = block.get("content", "")
                        if isinstance(output, list):
                            output = "\n".join(item.get("text", "") for item in output if isinstance(item, dict))
                        match = re.search(r"(?:Exit code:?|exit code|exited with code)\s*(\d+)", str(output), re.I)
                        code = int(match.group(1)) if match else (1 if block.get("is_error") else 0)
                        tool_event(events, tool[0], tool[1], output, code, tool[2],
                                   output if block.get("is_error") else None)
    for name, inputs, ts in tools.values():
        tool_event(events, name, inputs, "", ts=ts)
    if pool == "cc":
        events.sort(key=lambda event: moment(event.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return events


def cc_source(job):
    start = moment(job.get("started_at"))
    end = moment(job.get("finished_at")) or datetime.now(timezone.utc)
    if start is None:
        return None
    directory = Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", job.get("cwd", ""))
    candidates = []
    for path in directory.glob("*.jsonl"):
        if not safe_file(directory, path.name):
            continue
        try:
            # Only the prefix is needed to identify a session; tail parsing is separate.
            with path.open("rb") as stream:
                prefix = stream.read(MAX_BYTES).decode("utf-8", errors="replace")
            for line in prefix.splitlines():
                try:
                    record = json.loads(line)
                    stamp = moment(record.get("timestamp")) if isinstance(record, dict) else None
                except ValueError:
                    continue
                if stamp:
                    delta = (stamp - start).total_seconds()
                    if delta >= -10 and stamp <= end:
                        candidates.append((abs(delta), str(path), path))
                    break
        except OSError:
            continue
    return min(candidates)[2] if candidates else None


def job_events(job, jd, limit=300):
    directory = Path(jd)
    pool = job.get("pool")
    source = cc_source(job) if pool == "cc" else safe_file(directory, "output.jsonl" if pool == "opencode" else "log")
    events = []
    if source:
        text = read_tail(source)
        events = codex_events(text) if pool == "codex" else json_events(text, pool)
    if pool == "opencode":
        stderr = safe_file(directory, "log")
        for line in read_tail(stderr).splitlines() if stderr else []:
            try:
                json.loads(line)
            except ValueError:
                output_events(events, line)
    # An empty trace must remain empty so the UI can fall back to raw logs.
    if events:
        if job.get("started_at"):
            events.insert(0, dict(kind="start", text=pool or "", ts=job["started_at"]))
        conclusion = safe_file(directory, "conclusion.md")
        for line in read_tail(conclusion).splitlines() if conclusion else []:
            if line.strip().startswith("STATUS:"):
                emit(events, "end", redact(line.strip())[:200])
    result = dict(source=redact(source) if source else None, pool=pool, events=events[-max(0, limit):] if limit > 0 else [])
    if not events:
        result["note"] = "未找到匹配的 CC 会话（按 cwd 和启动时间启发式匹配）" if pool == "cc" else "暂无可解析的执行过程"
    return result
