"""Affinity + caps for cs CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_CAPS = {"codex": 24, "cc": 6, "opencode": 32}
# Import-time defaults; reload_caps() refreshes them from caps.json / env.
CODEX_CAP = DEFAULT_CAPS["codex"]
CC_CAP = DEFAULT_CAPS["cc"]
OPENCODE_CAP = DEFAULT_CAPS["opencode"]


def _positive_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, int) and value > 0:
        return value
    return None


def load_caps(root=None):
    """Resolve caps: {root}/caps.json, then CS_<POOL>_CAP env, then defaults."""
    if root is None:
        root = (os.environ.get("CS_HOME") or os.environ.get("CODING_SCHEDULER_HOME")
                or Path.home() / ".coding-scheduler")
    try:
        data = json.loads((Path(root).expanduser() / "caps.json").read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    caps = {}
    for key in DEFAULT_CAPS:
        env = "CS_%s_CAP" % key.upper()
        caps[key] = (_positive_int(data.get(key)) or _positive_int(os.environ.get(env))
                     or DEFAULT_CAPS[key])
    return caps


def reload_caps(root=None):
    """Refresh module-level caps; invalid entries fall through."""
    global CODEX_CAP, CC_CAP, OPENCODE_CAP
    caps = load_caps(root)
    CODEX_CAP, CC_CAP = caps["codex"], caps["cc"]
    OPENCODE_CAP = caps["opencode"]
    return caps

AREA = {
    "backend": (90, 40),
    "protocol": (90, 40),
    "frontend": (45, 85),
    "ui-copy": (45, 85),
    "test": (75, 55),
    "ops-script": (60, 60),
    "review": (50, 50),
    "research": (60, 60),
}
TIER = {"L": (50, 70), "M": (70, 55), "H": (80, 65)}


def score(job, running_counts, running_by_area):
    pin = job.get("pin")
    if pin == "codex":
        return 100, 0
    if pin == "cc":
        return 0, 100
    area = job.get("area") or "ops-script"
    tier = job.get("tier") or "M"
    risk = job.get("risk") or "low"
    ca, cc = AREA.get(area, (60, 60))
    ta, tc = TIER.get(tier, (60, 60))
    codex = int(0.6 * ca + 0.4 * ta)
    ccs = int(0.6 * cc + 0.4 * tc)
    if area == "review":
        ro = job.get("review_of")
        if ro == "codex":
            codex, ccs = 30, 80
        elif ro == "cc":
            codex, ccs = 80, 30
    if risk == "high" and area not in ("frontend", "ui-copy"):
        codex += 10
        ccs = max(0, ccs - 5)
    if running_counts.get('codex', 0) >= int(0.9 * CODEX_CAP):
        codex -= 25
    if running_counts.get('cc', 0) >= int(0.9 * CC_CAP):
        ccs -= 25
    if running_by_area.get(("codex", area), 0) >= 3:
        codex -= 15
    if running_by_area.get(("cc", area), 0) >= 3:
        ccs -= 15
    return max(0, min(100, codex)), max(0, min(100, ccs))


def pick_pool(codex_s, cc_s, free_codex, free_cc):
    """Return (pool, reasons); spill constraints are reported independently."""
    primary = "codex" if codex_s >= cc_s else "cc"
    secondary = "cc" if primary == "codex" else "codex"
    secondary_s = cc_s if primary == "codex" else codex_s
    primary_free = free_codex if primary == "codex" else free_cc
    secondary_free = free_cc if primary == "codex" else free_codex
    if primary_free > 0:
        return primary, []
    if secondary_free > 0 and secondary_s >= 45:
        return secondary, []
    context = {"primary": primary, "secondary": secondary,
               "scores": {"codex": codex_s, "cc": cc_s}}
    reasons = [dict(context, type="pool_full", pool=primary, free=primary_free)]
    if secondary_free <= 0:
        reasons.append(dict(context, type="pool_full", pool=secondary, free=secondary_free))
    if secondary_s < 45:
        reasons.append(dict(context, type="spill_insufficient", pool=secondary,
                            score=secondary_s, minimum=45))
    return None, reasons


def model_for(pool, tier):
    if pool == "opencode":
        return "opencode/deepseek-v4.1-flash-free", "max"
    effort = {"L": "low", "M": "medium", "H": "high"}.get(tier, "medium")
    if pool == "codex":
        return "gpt-6.1-sol", effort
    model = {
        "L": "murmurant/claude-haiku-5-5",
        "M": "murmurant/claude-sonnet-5-5",
        "H": "murmurant/claude-opus-5-5",
    }.get(tier, "murmurant/claude-sonnet-5-5")
    return model, effort
