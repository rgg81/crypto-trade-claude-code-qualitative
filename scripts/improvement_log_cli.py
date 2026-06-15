"""Append an OVERSEER improvement record to the dual-format journal (orchestration helper).

The autonomous self-improvement loop calls this AFTER it has decided what to do with an
:class:`~futures_fund.improvement.ImprovementProposal` — auto-applied a safe prompt/config/code fix
(``--applied``) or surfaced a protected / risk-critical finding to a human (``--surfaced``). It is a
thin, deterministic wrapper over :func:`futures_fund.improvement.record_improvement`, writing both
``memory/improvement-journal.md`` (human-readable) and ``memory/improvement-journal.jsonl``
(machine) atomically.

    uv run python scripts/improvement_log_cli.py \
        --detected "flow lane hallucinated 3 cycles running" \
        --classification prompt --target agents/flow_sentiment.md \
        --fix "tighten cite-only-real-ids rule with a counter-example" \
        --applied --test-result "778 passed"

    uv run python scripts/improvement_log_cli.py \
        --detected "recurring auditor advisory on narrative thin-evidence" \
        --classification protected --target futures_fund/sentiment_audit.py \
        --fix "surface to human — never auto-applied" --surfaced \
        --test-result "human review queued"

    # or pass a whole entry as JSON (e.g. from the orchestration):
    uv run python scripts/improvement_log_cli.py --json '{"detected": "...", "applied": true}'

The TIMESTAMP defaults to now() at the CLI boundary (the orchestration's wall clock) but can be
PINNED with ``--ts`` for a deterministic re-run; the library itself never reads the clock.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.improvement import record_improvement

_MEMORY_DIR = "memory"


def _build_entry(args: argparse.Namespace) -> dict:
    """Assemble the improvement entry from --json (if any) overlaid by the explicit flags.

    --json supplies a base dict (an entry serialized by the orchestration); the individual flags
    OVERRIDE matching keys when provided, so a caller can pass a JSON skeleton and tweak one field.
    """
    entry: dict = {}
    if args.json:
        loaded = json.loads(args.json)
        if isinstance(loaded, dict):
            entry.update(loaded)
    # explicit flags override the JSON base (only when the user actually set them).
    if args.detected is not None:
        entry["detected"] = args.detected
    if args.classification is not None:
        entry["classification"] = args.classification
    if args.target is not None:
        entry["target"] = args.target
    if args.fix is not None:
        entry["fix_summary"] = args.fix
    if args.test_result is not None:
        entry["test_result"] = args.test_result
    # applied / surfaced are store_true flags; only force them when set on the CLI so a --json
    # base's boolean is preserved when the flag is absent.
    if args.applied:
        entry["applied"] = True
    if args.surfaced:
        entry["surfaced"] = True
    return entry


def run(memory_dir, entry: dict, now: datetime) -> dict:
    """Append the entry to both journals; return a small JSON-able summary."""
    paths = record_improvement(memory_dir, entry, ts=now)
    return {
        "logged": True,
        "ts": now.isoformat(),
        "target": entry.get("target", ""),
        "applied": bool(entry.get("applied", False)),
        "surfaced": bool(entry.get("surfaced", False)),
        "md": str(paths["md"]),
        "jsonl": str(paths["jsonl"]),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Append an OVERSEER improvement record (wraps record_improvement)."
    )
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    ap.add_argument("--detected", help="the systematic problem detected")
    ap.add_argument("--classification", choices=["prompt", "config", "code", "protected"])
    ap.add_argument("--target", help="the EXACT file path the fix targets")
    ap.add_argument("--fix", help="the concrete change made / proposed (fix_summary)")
    ap.add_argument("--test-result", dest="test_result",
                    help="verification result, e.g. '778 passed'")
    ap.add_argument("--applied", action="store_true", help="the fix was AUTO-APPLIED")
    ap.add_argument("--surfaced", action="store_true", help="the finding was SURFACED to a human")
    ap.add_argument("--json", help="a full entry dict as JSON (flags override matching keys)")
    ap.add_argument("--ts", help="ISO-8601 timestamp to PIN the record (default: now, UTC)")
    args = ap.parse_args(argv)

    now = datetime.fromisoformat(args.ts) if args.ts else datetime.now(UTC)
    entry = _build_entry(args)
    out = run(args.memory_dir, entry, now)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
