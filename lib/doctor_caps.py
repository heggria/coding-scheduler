"""Evidence-only capability snapshots; no probes, writes or scheduler policy."""
from __future__ import annotations

CAPABILITIES = ("cli_pool_ready", "cwd_git_writable", "resume_supported",
                "network_dns", "network_access", "browser_or_mcp", "auth")


def normalize_capabilities(capabilities=None):
    result = {name: {"status": "unknown", "detail": "not checked"} for name in CAPABILITIES}
    for name, evidence in (capabilities or {}).items():
        if name not in result or not isinstance(evidence, dict):
            raise ValueError("invalid capability: " + name)
        if evidence.get("status") not in ("ok", "unavailable", "unknown"):
            raise ValueError("invalid capability status: " + name)
        result[name] = {"status": evidence["status"], "detail": str(evidence.get("detail", ""))}
    return result


def doctor_capabilities(checks=(), pool=None):
    """Reuse doctor observations. DNS success proves resolution, not connectivity/auth."""
    result = normalize_capabilities()
    indexed = {check["name"]: check for check in checks}

    def copy(name, check_name):
        check = indexed.get(check_name)
        if check is not None and isinstance(check.get("ok"), bool):
            result[name] = {"status": "ok" if check["ok"] else "unavailable",
                            "detail": str(check.get("detail", check_name))}

    cli = {"codex": "codex_cli", "cc": "claude_cli", "opencode": "opencode_cli"}
    if pool in cli:
        copy("cli_pool_ready", cli[pool])
        result["resume_supported"] = {
            "status": "ok" if pool in ("codex", "cc") else "unavailable",
            "detail": "scheduler pool protocol only; original job/session eligibility still requires submit validation",
        }
    copy("network_dns", "github_dns")
    if indexed.get("cwd_exists", {}).get("ok") is False:
        copy("cwd_git_writable", "cwd_exists")
    elif "git_writable" in indexed:
        check = indexed["git_writable"]
        if check.get("ok") is False and check.get("level") == "WARN":
            # Doctor also uses this check for an unsuccessful git invocation.
            result["cwd_git_writable"]["detail"] = str(check.get("detail", "git probe inconclusive"))
        else:
            copy("cwd_git_writable", "git_writable")
    elif indexed.get("git_repo", {}).get("ok") is False:
        copy("cwd_git_writable", "git_repo")
    return result
