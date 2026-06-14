"""Reflection CLI: emit the winners/losers/declined payload for the Reflector subagent.

Forked from the base desk's reflect_cli, with the dropped `orchestration` import stripped — the
qualitative desk calls :func:`futures_fund.reflect.reflection_payload` directly. The payload
contrasts closed WINNERS vs LOSERS (which trades worked) AND the edge-aligned FLAT decisions the
desk declined (so reflection can mint ENABLING 'DO take it when X' lessons, not only prohibitions).
It is persisted to ``state/cycle/<N>/reflection_input.json`` for the Reflector to read.

    uv run python scripts/reflect_cli.py --cycle N
    uv run python scripts/reflect_cli.py --cycle N --memory-dir memory --state-dir state
"""
from __future__ import annotations

import argparse
import json
import sys

from futures_fund.cycle_io import save_output
from futures_fund.reflect import reflection_payload

_STATE_DIR = "state"
_MEMORY_DIR = "memory"


def build(state_dir, memory_dir, cycle: int) -> dict:
    """Assemble + persist this cycle's reflection input.

    Reads the episodic journal + flat-decision log under `memory_dir` (winners/losers/declined/
    missed) and writes ``state/cycle/<cycle>/reflection_input.json``. Returns the payload (also what
    is written) so a caller/test can assert on it without re-reading the file."""
    payload = reflection_payload(memory_dir)
    save_output(state_dir, cycle, "reflection_input", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Emit the Reflector's winners/losers/declined data.")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    args = ap.parse_args(argv)
    payload = build(args.state_dir, args.memory_dir, args.cycle)
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
