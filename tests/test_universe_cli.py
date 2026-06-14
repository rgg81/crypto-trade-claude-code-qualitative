"""Offline tests for scripts/universe_cli.py.

The universe = config core_watchlist ∪ attention-spiking coins (from on-disk digests) ∪ every
currently-held position. The held names are load-bearing: they MUST never drop out, even when a
held coin is neither core nor spiking. Everything here is pure on-disk I/O — no network."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.content_store import _digest_path
from futures_fund.cycle_io import cycle_dir
from futures_fund.state import Position, save_positions
from scripts.universe_cli import build, held_coins, load_digests

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _write_digest(content_dir: Path, coin: str, vol_24h: float, baseline: float) -> None:
    path = _digest_path(content_dir, coin)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "coin": coin.upper(),
        "mention_volume_24h": vol_24h,
        "mention_volume_baseline": baseline,
    }))


def _position(symbol: str) -> Position:
    return Position(
        symbol=symbol,
        direction="long",
        qty=1.0,
        entry=100.0,
        stop=90.0,
        take_profits=[120.0],
        leverage=3.0,
        margin=33.0,
        liq_price=70.0,
        opened_cycle=1,
        opened_ts=NOW,
    )


def _build(state_dir, content_dir, *, core, ratio=2.0, min_mentions=5, cycle=7):
    return build(
        str(state_dir), str(content_dir), cycle, NOW,
        core=core, ratio=ratio, min_mentions=min_mentions,
    )


# --- core ∪ spiking ∪ held, de-duplicated ----------------------------------


def test_union_core_spiking_held_no_dups(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _write_digest(content_dir, "DOGE", vol_24h=30, baseline=1.0)   # spikes
    _write_digest(content_dir, "SOL", vol_24h=2, baseline=5.0)     # does NOT spike
    save_positions(state_dir, [_position("AVAXUSDT")])             # held, not core/spiking

    u = _build(state_dir, content_dir, core=["BTC", "ETH"])
    assert u["core"] == ["BTC", "ETH"]
    assert u["spiking"] == ["DOGE"]              # SOL excluded (no spike)
    assert u["held"] == ["AVAX"]
    assert u["all"] == ["AVAX", "BTC", "DOGE", "ETH"]   # sorted unique union
    # no duplicates anywhere
    assert len(u["all"]) == len(set(u["all"]))


def test_held_always_included_even_when_not_spiking_or_core(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    # No digests at all -> spiking empty. WIF is held but neither core nor spiking.
    save_positions(state_dir, [_position("WIFUSDT")])

    u = _build(state_dir, content_dir, core=["BTC"])
    assert u["spiking"] == []
    assert u["held"] == ["WIF"]
    assert "WIF" in u["all"]                     # the never-drop-a-held-name invariant
    assert u["all"] == ["BTC", "WIF"]


def test_held_that_is_also_core_is_not_duplicated(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    save_positions(state_dir, [_position("BTCUSDT")])  # held AND core

    u = _build(state_dir, content_dir, core=["BTC", "ETH"])
    assert u["held"] == ["BTC"]
    assert u["all"].count("BTC") == 1
    assert u["all"] == ["BTC", "ETH"]


def test_multiple_held_positions_unified_symbols(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    save_positions(state_dir, [_position("ETH/USDT:USDT"), _position("LINKUSDT")])

    u = _build(state_dir, content_dir, core=["BTC"])
    assert u["held"] == ["ETH", "LINK"]
    assert set(u["all"]) == {"BTC", "ETH", "LINK"}


def test_no_positions_file_yields_empty_held(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _write_digest(content_dir, "PEPE", vol_24h=40, baseline=1.0)

    u = _build(state_dir, content_dir, core=["BTC"])
    assert u["held"] == []
    assert u["all"] == ["BTC", "PEPE"]


# --- persistence -----------------------------------------------------------


def test_universe_written_to_cycle_dir(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    save_positions(state_dir, [_position("AVAXUSDT")])

    u = _build(state_dir, content_dir, core=["BTC"], cycle=42)
    written = json.loads((cycle_dir(str(state_dir), 42) / "universe.json").read_text())
    assert written == u
    assert set(written) == {"core", "spiking", "held", "all"}


# --- fail-soft digest reading ----------------------------------------------


def test_torn_digest_is_skipped_not_fatal(tmp_path: Path) -> None:
    content_dir = tmp_path / "content"
    _write_digest(content_dir, "DOGE", vol_24h=30, baseline=1.0)
    bad = _digest_path(content_dir, "JUNK")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{ not json")  # torn file
    digests = load_digests(str(content_dir))
    assert "DOGE" in digests
    assert "JUNK" not in digests   # unreadable digest skipped, never raised


def test_missing_digests_dir_is_empty(tmp_path: Path) -> None:
    assert load_digests(str(tmp_path / "content")) == {}


def test_held_coins_fail_soft_on_torn_positions(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "positions.json").write_text("{ not json")
    assert held_coins(str(state_dir)) == []
