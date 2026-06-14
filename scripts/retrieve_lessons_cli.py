"""Retrieve regime-relevant lessons for the debate/decider prompts (qualitative desk).

Forked from the base desk's retrieve_lessons_cli with the dropped `orchestration` import stripped —
the qualitative desk calls :func:`futures_fund.lessons.retrieve_lessons` directly. The retrieval is
regime-FILTERED FIRST (a lesson tagged for a regime that doesn't match the query is dropped; a
lesson with regime None/'any' always applies) then ranked + polarity-quota-balanced so the injected
set is two-sided. Output is persisted to ``state/cycle/<N>/lessons.json`` as ``{"lessons": [...]}``.

`--regime` accepts a COMMA-SEPARATED query context — for the sentiment desk pass BOTH the crowd
MOOD and the SOURCE-MIX label, e.g. ``--regime "euphoric,social-heavy"`` — so a lesson tagged for
either context surfaces (a single value stays a plain string for back-compat).

    uv run python scripts/retrieve_lessons_cli.py --cycle N --regime "euphoric,social-heavy" --k 5
    uv run python scripts/retrieve_lessons_cli.py --cycle N --regime fearful --tags funding --k 8
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.cycle_io import save_output
from futures_fund.lessons import retrieve_lessons

_STATE_DIR = "state"
_MEMORY_DIR = "memory"


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def build(state_dir, memory_dir, cycle: int, now: datetime, *, regime, tags: list[str],
          k: int) -> dict:
    """Retrieve + persist this cycle's top-K regime-relevant lessons.

    `regime` is the query CONTEXT(S): a single string OR an iterable (mood + source-mix). Writes
    ``state/cycle/<cycle>/lessons.json`` as ``{"lessons": [<lesson-dict>, ...]}`` and returns it."""
    lessons = [
        lz.model_dump(mode="json")
        for lz in retrieve_lessons(memory_dir, now, regime, tags, k)
    ]
    payload = {"lessons": lessons}
    save_output(state_dir, cycle, "lessons", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Retrieve regime-relevant lessons for a cycle.")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--regime", default=None,
                    help="comma-separated query context(s): pass BOTH the crowd MOOD and the "
                         "SOURCE-MIX label, e.g. 'euphoric,social-heavy'")
    ap.add_argument("--tags", default="")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    ap.add_argument("--now", default=None, help="ISO timestamp (UTC); defaults to now")
    args = ap.parse_args(argv)

    tags = [t for t in args.tags.split(",") if t]
    # accept comma-separated contexts; a single value stays a plain string (back-compat)
    ctx = [r for r in (args.regime or "").split(",") if r] or None
    regime = ctx[0] if ctx and len(ctx) == 1 else ctx

    payload = build(args.state_dir, args.memory_dir, args.cycle, _parse_now(args.now),
                    regime=regime, tags=tags, k=args.k)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
