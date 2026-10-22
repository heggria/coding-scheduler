#!/usr/bin/env python3
"""Read-only copy of production state, then a fake-worker-only CLI smoke."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

REPO = Path(__file__).resolve().parents[1]


def main():
    source = Path(os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME")
                  or Path.home() / ".coding-scheduler").expanduser().resolve()
    smoke_dir = Path(tempfile.mkdtemp(prefix="cs-AD-smoke-", dir="/tmp")).resolve()
    root = smoke_dir / "state"
    if source.exists():
        # Dereference links into independent copies, never reuse a writable link.
        shutil.copytree(source, root, symlinks=False)
    else:
        root.mkdir()
    archive = root / "inherited-snapshot"
    archive.mkdir(exist_ok=True)
    # Never schedule or signal copied production jobs/PIDs. Only fresh fake jobs.
    for name in ("queue", "running", "done", "quota.json", "logs"):
        path = root / name
        if path.exists():
            path.rename(archive / name)
    cwd = smoke_dir / "worktree"
    cwd.mkdir()
    env = dict(os.environ, CS_HOME=str(root), CS_TEST_FAKE_WORKER="1")
    for args in (["init"], ["-c", "user.name=Smoke", "-c", "user.email=smoke@example.invalid",
                            "commit", "--allow-empty", "-m", "smoke base"]):
        subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, timeout=10)
    records = []

    def cs(*args):
        result = subprocess.run(["/usr/bin/python3", str(REPO / "bin/cs"), *args, "--json"],
                                env=env, capture_output=True, text=True, timeout=10)
        (smoke_dir / (args[0] + ".json")).write_text(result.stdout)
        assert result.returncode == 0, (args[0], result.returncode, result.stderr)
        data = json.loads(result.stdout)
        assert data["schema_version"] == "1"
        records.append({"command": args[0], "exit": result.returncode})
        return data

    jid = "cs-ad-smoke"
    submitted = cs("submit", "--project", "cs", "--area", "research", "--read-only", "--tier", "L",
                   "--cwd", str(cwd), "--id", jid, "--prompt-text", "Only run the fake worker.",
                   "--no-template", "--idempotency-key", "cs-ad-smoke", "--no-tick")
    assert submitted["job"]["status"] == "queued"
    assert submitted["job"]["queue_reasons"] == [{"type": "awaiting_tick"}]
    assert cs("tick")["started"] == [jid]
    waited = cs("wait", jid, "--timeout", "5", "--lines", "2")
    assert not waited["timed_out"] and waited["jobs"][0]["status"] == "done"
    assert waited["jobs"][0]["exit"] == 0
    assert waited["jobs"][0]["changed_files"] == []
    conclusion = Path(waited["jobs"][0]["artifacts"]["conclusion"])
    assert conclusion == root / "workers" / jid / "conclusion.md"
    assert not (cwd / "conclusion.md").exists()
    assert cs("status", jid)["job"]["status"] == "done"
    second = cs("submit", "--project", "cs", "--area", "review", "--read-only", "--tier", "L",
                "--cwd", str(cwd), "--id", "wait-tick", "--prompt-text", "Only fake worker.",
                "--no-template", "--files", str(cwd / "review.md"), "--no-tick")
    assert second["job"]["status"] == "queued"
    assert cs("wait", "wait-tick", "--tick", "--timeout", "5")["jobs"][0]["status"] == "done"
    board = Path(cs("board")["path"])
    stats = cs("stats", "--since", "2h", "--project", "cs")
    assert stats["total"] == 2 and stats["success_rate"] == 1
    assert stats["distribution"]["done"] == 2
    events = [json.loads(line) for line in (root / "logs/events.jsonl").read_text().splitlines()]
    assert len(events) == 2 and {e["id"] for e in events} == {jid, "wait-tick"}
    assert all(e["prompt_bytes"] > 0 for e in events)
    assert "原因" in board.read_text()
    assert (root / "workers" / jid / "log").read_text() == "test fake worker\n"
    assert board == root / "board.md"
    assert (root / ".lock").is_file()
    for job_id in (jid, "wait-tick"):
        job_path = root / "done" / (job_id + ".json")
        job = json.loads(job_path.read_text())
        worker = root / "workers" / job_id
        for field, name in (("prompt_file", "prompt.md"), ("log", "log"),
                            ("conclusion", "conclusion.md")):
            assert Path(job[field]) == worker / name
            assert Path(job[field]).is_file()
        assert not (root / "queue" / (job_id + ".prompt.md")).exists()
        assert not (root / "running" / (job_id + ".json")).exists()
    report = {"source_exists": source.exists(), "cs_home_isolated": True,
              "fake_worker": True, "directory": str(smoke_dir), "steps": records}
    (smoke_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
