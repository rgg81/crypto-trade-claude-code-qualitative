"""Due-gate CLI for the 15-minute CRAWLER loop (Operation ORACLE).

The crawler cron polls frequently; this is the FIRST action each fire. It decides whether the
current N-minute grid slot still needs a crawl tick and prints ONE of:

    DUE: <reason>   -> run one crawl tick (scripts/crawl_cli.py); exit 0
    SKIP: <reason>  -> this slot is already served; exit quietly (a liveness ping); exit 0

Gating is on the 15-minute slot containing ``now`` vs the last slot served in
``state/crawl/last_crawl.json`` (see :func:`futures_fund.scheduling.crawl_due`): run iff now's slot
is strictly later than the last served slot. Fail-safe DUE on any internal error — an extra tick is
low-harm (it dedupes into the content store) whereas a swallowed slot loses tape. Makes ZERO
network calls and ZERO writes; always exits 0 (the loop never wedges on this gate).

    uv run python scripts/crawl_due_check.py                       # state/, 15-min grid, now=UTC
    uv run python scripts/crawl_due_check.py --state-dir state --interval-min 15
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="15-minute crawler-loop due-gate.")
    parser.add_argument("--state-dir", default="state", help="state directory (default: state)")
    parser.add_argument("--interval-min", type=int, default=15,
                        help="crawl grid interval in minutes (default: 15)")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        from futures_fund.scheduling import crawl_due
        mode, reason = crawl_due(args.state_dir, datetime.now(UTC), interval_min=args.interval_min)
    except Exception as e:  # noqa: BLE001 — import/now failed; fail SAFE to DUE but stay visible
        print(f"DUE: crawl_due_check failed before decision -> fail-safe DUE: {e!r}")
        return 0

    print(f"{mode}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
