"""Universe builder CLI for the Operation ORACLE decision loop.

Builds this cycle's working universe = the static config ``core_watchlist`` UNION the
attention-spiking coins detected over all on-disk per-coin digests, PLUS every symbol the desk is
CURRENTLY HOLDING. The held set is load-bearing: a coin we hold must NEVER drop out of the universe
just because it stopped spiking — it has to flow through evidence/decision so the desk can manage or
exit it. (`build_universe` covers core+spiking; this CLI folds held on top.)

    uv run python scripts/universe_cli.py --cycle N
    uv run python scripts/universe_cli.py --cycle N --now 2026-06-13T12:00:00Z

Reads config.yaml (core_watchlist + spike thresholds), every ``content/digests/*.json`` and
``state/positions.json``; writes the atomic JSON ``state/cycle/<N>/universe.json``::

    {"core": [...], "spiking": [...], "held": [...], "all": [...]}

All lists are sorted, de-duplicated upper-case base tickers. Pure I/O over the content store and
on-disk state — makes ZERO network/exchange calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.config import load_settings
from futures_fund.content_store import _digests_dir
from futures_fund.cycle_io import save_output
from futures_fund.sentiment_audit import _coin_of
from futures_fund.state import load_positions
from futures_fund.universe import build_universe

_STATE_DIR = "state"
_CONTENT_DIR = "content"


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def load_digests(content_dir) -> dict[str, dict]:
    """Read every ``content/digests/<COIN>.json`` into a ``{COIN: digest}`` map.

    Fail-soft per file: a missing digests dir yields {} and a torn/non-dict digest file is skipped,
    so a single corrupt digest can never wedge universe construction. The coin key is the file stem
    upper-cased (the store names digests by upper-case base ticker)."""
    digests: dict[str, dict] = {}
    d = Path(_digests_dir(content_dir))
    if not d.exists():
        return digests
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue  # torn/unreadable digest — skip, never crash the cycle
        if isinstance(data, dict):
            digests[path.stem.upper()] = data
    return digests


def held_coins(state_dir) -> list[str]:
    """Base tickers of every currently-held position, sorted & de-duped (e.g. BTCUSDT -> BTC).

    These MUST survive into the universe regardless of spike state so the desk can manage/exit
    them. Fail-soft: an unreadable positions file degrades to no held coins (the cycle still runs
    on core+spiking) rather than aborting."""
    try:
        positions = load_positions(state_dir)
    except Exception:  # noqa: BLE001 — a torn positions file must not crash universe build
        return []
    return sorted({_coin_of(p.symbol).upper() for p in positions})


def build(
    state_dir,
    content_dir,
    cycle: int,
    now: datetime,
    *,
    core: list[str],
    ratio: float,
    min_mentions: int,
) -> dict[str, list[str]]:
    """Assemble {core, spiking, held, all} and persist it as the cycle's universe.json.

    `all` is the sorted unique union of core + spiking + held — so a held coin that is neither core
    nor spiking is still IN the working universe (the never-drop-a-held-name invariant)."""
    digests = load_digests(content_dir)
    base = build_universe(core, digests, now, ratio=ratio, min_mentions=min_mentions)
    held = held_coins(state_dir)
    universe = {
        "core": base["core"],
        "spiking": base["spiking"],
        "held": held,
        "all": sorted(set(base["all"]) | set(held)),
    }
    save_output(state_dir, cycle, "universe", universe)
    return universe


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the cycle universe (core ∪ spiking ∪ held).")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--content-dir", default=_CONTENT_DIR)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--now", default=None, help="ISO timestamp (UTC); defaults to now")
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    q = settings.qualitative
    universe = build(
        args.state_dir,
        args.content_dir,
        args.cycle,
        _parse_now(args.now),
        core=q.core_watchlist,
        ratio=q.spike_ratio,
        min_mentions=q.spike_min_mentions,
    )
    print(json.dumps(universe, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
