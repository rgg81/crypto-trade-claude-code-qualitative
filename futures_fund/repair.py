from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# Safety-critical modules: a self-healing "fix" may NEVER weaken a risk or execution limit
# here (spec §5). The orchestrator must keep the full test suite green before committing a
# change to any of these, and HALT rather than bypass a limit it cannot fix safely.
# `cycle_io` is the REAL per-cycle output writer; the legacy `cycle` stem is kept so an old
# reference still matches. The qualitative-desk risk modules (the Auditor `sentiment_audit`, the
# reward/risk floor `rr_floor`, the invariant checker `self_audit`, the portfolio-heat accountant
# `portfolio_risk`, the trigger-geometry / per-trade-risk plumbing `pending_orders`, the
# position-risk state `state`) are ALSO protected here so the SAME gate sees them no matter which
# subsystem asks. Names are matched CASE-INSENSITIVELY and against EVERY dotted component of the
# filename (so a `risk_gate.py.bak` / `sentiment_audit.bak.py` backup is caught too).
PROTECTED_PATHS = ("risk_gate", "executor", "exits", "consolidation", "policy",
                   "liquidation", "sizing", "cycle", "cycle_io",
                   "sentiment_audit", "rr_floor", "self_audit", "portfolio_risk",
                   "pending_orders", "state")

_PROTECTED_CF = frozenset(p.casefold() for p in PROTECTED_PATHS)


def _name_components(path: str) -> set[str]:
    """Every casefolded dotted component of the filename.

    ``futures_fund/risk_gate.py``    -> {"risk_gate", "py"}
    ``futures_fund/risk_gate.py.bak`` -> {"risk_gate", "py", "bak"}
    ``futures_fund/risk_gate.bak.py`` -> {"risk_gate", "bak", "py"}

    Splitting on every dot (not just the stem) defeats multi-dot evasion: a protected name
    survives as one of the components regardless of how many suffixes are appended."""
    return {part.casefold() for part in Path(path).name.split(".") if part}


def is_protected(path: str) -> bool:
    """True if `path` is one of the risk/execution-critical modules.

    Matching is CASE-INSENSITIVE and considers EVERY dotted component of the filename, so
    ``Risk_Gate.py`` and ``risk_gate.py.bak`` are both flagged."""
    return bool(_name_components(path) & _PROTECTED_CF)


def log_error(state_dir, *, phase: str, command: str, error: str,
              ts: datetime, traceback: str = "") -> Path:
    """Append a structured error record to state/error-log.jsonl (no silent failures)."""
    p = Path(state_dir) / "error-log.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": ts.isoformat(), "phase": phase, "command": command,
           "error": error, "traceback": traceback[:2000]}
    with p.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return p


def record_repair(memory_dir, *, symptom: str, root_cause: str, fix: str,
                  verification: str, ts: datetime) -> Path:
    """Append an auditable repair entry to memory/repair-journal.md (committed)."""
    p = Path(memory_dir) / "repair-journal.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(f"\n## {ts:%Y-%m-%d %H:%M} repair\n"
                f"- **Symptom:** {symptom}\n"
                f"- **Root cause:** {root_cause}\n"
                f"- **Fix:** {fix}\n"
                f"- **Verification:** {verification}\n")
    return p
