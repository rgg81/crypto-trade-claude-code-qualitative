"""Hourly-poll due-gate CLI for the Operation ORACLE loop.

The cron polls hourly; this is the FIRST action each fire. It decides whether the current 4h
candle still needs a cycle and prints ONE of:

    DUE FRESH <N>   -> run a brand-new cycle end-to-end; create state/cycle/<N>/
    DUE RETRY <N>   -> a prior dir crashed before the gate; re-run/OVERWRITE state/cycle/<N>/
    SKIP: <reason>  -> this candle is already served; exit quietly (the line is a liveness ping)
    ERROR: <reason> -> internal failure (exit code 2); do NOT trade, surface/notify

Exit code: 0 for DUE*/SKIP, 2 for ERROR. Makes ZERO exchange/network calls and ZERO writes — it
only delegates to :func:`futures_fund.scheduling.cycle_due`.

    uv run python scripts/due_check.py            # uses state/ and now=UTC
    uv run python scripts/due_check.py <state_dir> # explicit state dir (testing)
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    state_dir = argv[0] if argv else "state"
    try:
        from futures_fund.scheduling import cycle_due
        mode, n, reason = cycle_due(state_dir, datetime.now(UTC))
    except Exception as e:  # noqa: BLE001 — the import/now itself failed; fail SAFE but visible
        print(f"ERROR: due_check failed before decision: {e!r}")
        return 2
    if mode in ("FRESH", "RETRY"):
        print(f"DUE {mode} {n}")
        print(reason)
        return 0
    print(f"SKIP: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
