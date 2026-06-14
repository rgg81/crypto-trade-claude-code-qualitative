"""Standalone retention purge for the 30-day content store (Operation ORACLE).

The crawl tick already purges every run; this is the explicit, schedulable wrapper (e.g. a nightly
cron) that evicts everything older than the retention window via
:func:`futures_fund.content_store.purge`. Day-granular eviction: whole day files strictly before the
cutoff day are deleted and matching index pointers dropped (the function keeps get_item/index in
agreement). Prints a one-line result. Exit 0 on success, 1 on a hard failure.

    uv run python scripts/purge_cli.py
    uv run python scripts/purge_cli.py --content-dir content --retain-days 30
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from futures_fund import content_store


def run_purge(content_dir: str, retain_days: int, now: datetime) -> dict:
    """Evict past the retention window; returns ``{files_deleted, pointers_dropped}``."""
    return content_store.purge(content_dir, now, retain_days=retain_days)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge the content store past its retention window.")
    parser.add_argument("--content-dir", default="content")
    parser.add_argument("--retain-days", type=int, default=30)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        out = run_purge(args.content_dir, args.retain_days, datetime.now(UTC))
    except Exception as e:  # noqa: BLE001 — surface a hard purge failure, exit non-zero
        print(f"PURGE ERROR: {e!r}")
        return 1

    print(f"purge ok: files_deleted={out['files_deleted']} "
          f"pointers_dropped={out['pointers_dropped']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
