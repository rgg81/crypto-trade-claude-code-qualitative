"""Deterministically persist the Reflector's minted lessons to the corpus (qualitative desk).

The reflect phase must ALWAYS append — never rely on the LLM Reflector subagent to remember to call
the lesson store itself (it did one cycle and not the next). This CLI reads the Reflector's JSON
output (``state/cycle/<N>/reflection_output.json`` by default) and appends each candidate lesson via
:func:`futures_fund.reflect.record_lessons`, which is IDEMPOTENT by exact text — re-running the same
cycle (a DUE RETRY) appends each lesson exactly once. Tolerates both a bare ``[...]`` list and a
``{"lessons": [...]}`` wrapper; tolerates the key drift ("lesson"/"rule"/"insight") record_lessons
itself absorbs.

Forked from the base desk's record_lessons_cli; no dropped-module imports.

    uv run python scripts/record_lessons_cli.py --cycle N
    uv run python scripts/record_lessons_cli.py --cycle N --input reflection_output
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.cycle_io import load_output
from futures_fund.reflect import record_lessons

_STATE_DIR = "state"
_MEMORY_DIR = "memory"
_INPUT = "reflection_output"


def _coerce_lessons(payload) -> list[dict]:
    """Normalise the Reflector's output to a list of lesson dicts.

    The documented contract is ``{"lessons": [...]}``, but a bare list or a
    ``{"candidate_lessons": [...]}`` mis-wrap are tolerated — fail SAFE (empty list) on anything
    un-coercible so a single mis-shape never aborts the reflect stage."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("lessons", "candidate_lessons", "candidates"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [x for x in inner if isinstance(x, dict)]
    return []


def run(state_dir, memory_dir, cycle: int, now: datetime, *, input_name: str = _INPUT) -> dict:
    """Read the Reflector's lessons for `cycle` and idempotently append them to the corpus.

    Returns ``{cycle, appended, lesson_ids}``. Missing input file -> nothing appended (the reflect
    phase is best-effort: a cycle with no minted lessons is a no-op, not an error)."""
    try:
        payload = load_output(state_dir, cycle, input_name)
    except FileNotFoundError:
        payload = {}
    lessons = _coerce_lessons(payload)
    ids = record_lessons(memory_dir, lessons, ts=now)
    return {"cycle": cycle, "appended": len(ids), "lesson_ids": ids}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Persist the Reflector's minted lessons (idempotent).")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    ap.add_argument("--input", default=_INPUT,
                    help="cycle output name holding the Reflector's lessons JSON")
    args = ap.parse_args(argv)
    out = run(args.state_dir, args.memory_dir, args.cycle, datetime.now(UTC),
              input_name=args.input)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
