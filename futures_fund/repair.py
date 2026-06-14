from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

# Safety-critical modules: a self-healing "fix" may NEVER weaken a risk or execution limit
# here (spec §5). The orchestrator must keep the full test suite green before committing a
# change to any of these, and HALT rather than bypass a limit it cannot fix safely.
PROTECTED_PATHS = ("risk_gate", "executor", "exits", "consolidation", "policy",
                   "liquidation", "sizing", "cycle")


def is_protected(path: str) -> bool:
    """True if `path` is one of the risk/execution-critical modules."""
    return Path(path).stem in PROTECTED_PATHS


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
