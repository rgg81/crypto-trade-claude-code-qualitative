"""Offline tests for scripts/evidence_cli.py.

Evidence assembly reads the cycle's universe.json and builds one packet per coin via an INJECTED
price_fn — so the whole thing runs with NO live network. The price card is RISK PLUMBING only
(non-directional note), and a price fetch that raises for ONE coin must degrade just that coin's
card (price_card.mark None) while every other coin still gets a full packet — the cycle is never
aborted."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from futures_fund.content_store import ContentItem, make_id, store_items, update_digest
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.decision_io import PRICE_CARD_NOTE, PRICE_CARD_UNAVAILABLE_NOTE
from scripts.evidence_cli import _atr_from_ohlcv, build, make_paper_price_fn, symbol_for_coin

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)

_MARKS = {"BTC": 65000.0, "ETH": 3500.0, "DOGE": 0.15}
_ATRS = {"BTC": 1200.0, "ETH": 80.0, "DOGE": 0.004}


def _fake_price_fn(coin: str) -> tuple[float, float]:
    return (_MARKS[coin], _ATRS[coin])


def _seed_universe(state_dir: Path, cycle: int, coins: list[str]) -> None:
    save_output(str(state_dir), cycle, "universe", {
        "core": sorted(coins), "spiking": [], "held": [], "all": sorted(coins),
    })


def _seed_item(content_dir: Path, coin: str, title: str) -> None:
    item = ContentItem(
        id=make_id("coindesk", f"https://x/{coin}", title),
        source="coindesk",
        feed="https://x/rss",
        url=f"https://x/{coin}",
        title=title,
        coins=[coin.upper()],
        published_ts=NOW - timedelta(hours=3),
        fetched_ts=NOW,
        item_sentiment="positive",
    )
    store_items(content_dir, [item])
    update_digest(content_dir, coin.upper(), NOW)


# --- happy path: packets built from injected price_fn (offline) ------------


def test_writes_packets_for_every_universe_coin(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_universe(state_dir, 3, ["BTC", "ETH"])
    _seed_item(content_dir, "BTC", "Bitcoin rips higher")

    out = build(str(state_dir), str(content_dir), 3, NOW, price_fn=_fake_price_fn)

    assert set(out) == {"BTC", "ETH"}
    btc = out["BTC"]
    assert btc["coin"] == "BTC"
    assert btc["price_card"]["mark"] == 65000.0
    assert btc["price_card"]["atr"] == 1200.0
    # the directional evidence rode along (digest + at least one recent item)
    assert btc["recent_items"]
    assert btc["digest"].get("coin") == "BTC"
    # ETH has no items but still gets a packet with a price card
    assert out["ETH"]["price_card"]["mark"] == 3500.0
    assert out["ETH"]["recent_items"] == []


def test_price_card_carries_non_directional_note(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_universe(state_dir, 1, ["BTC"])

    out = build(str(state_dir), str(content_dir), 1, NOW, price_fn=_fake_price_fn)
    assert out["BTC"]["price_card"]["note"] == PRICE_CARD_NOTE
    assert "NON-DIRECTIONAL" in out["BTC"]["price_card"]["note"]


def test_evidence_written_to_cycle_dir(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_universe(state_dir, 9, ["BTC", "ETH"])

    out = build(str(state_dir), str(content_dir), 9, NOW, price_fn=_fake_price_fn)
    written = json.loads((cycle_dir(str(state_dir), 9) / "evidence.json").read_text())
    assert written == out
    assert set(written) == {"BTC", "ETH"}


# --- fail-soft: a price_fn that raises for ONE coin degrades only that coin -


def test_price_fetch_failure_for_one_coin_degrades_gracefully(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_universe(state_dir, 5, ["BTC", "ETH", "DOGE"])
    _seed_item(content_dir, "DOGE", "Dogecoin surges on hype")

    def flaky_price_fn(coin: str) -> tuple[float, float]:
        if coin == "ETH":
            raise RuntimeError("market data timeout for ETH")
        return (_MARKS[coin], _ATRS[coin])

    # The whole cycle must NOT abort just because ETH's price fetch blew up.
    out = build(str(state_dir), str(content_dir), 5, NOW, price_fn=flaky_price_fn)
    assert set(out) == {"BTC", "ETH", "DOGE"}

    # ETH degrades to an unavailable card — never raised into the loop.
    assert out["ETH"]["price_card"]["mark"] is None
    assert out["ETH"]["price_card"]["atr"] is None
    assert out["ETH"]["price_card"]["note"] == PRICE_CARD_UNAVAILABLE_NOTE

    # The other coins are fully intact, evidence unaffected.
    assert out["BTC"]["price_card"]["mark"] == 65000.0
    assert out["DOGE"]["price_card"]["mark"] == 0.15
    assert out["DOGE"]["recent_items"]              # DOGE's qualitative evidence survived


def test_empty_universe_writes_empty_evidence(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_universe(state_dir, 2, [])
    out = build(str(state_dir), str(content_dir), 2, NOW, price_fn=_fake_price_fn)
    assert out == {}
    assert json.loads((cycle_dir(str(state_dir), 2) / "evidence.json").read_text()) == {}


# --- paper price_fn over a FAKE exchange (offline) -------------------------


class _FakeExchange:
    """Minimal offline stand-in for FuturesExchange: serves canned OHLCV + mark, no network."""

    def __init__(self, ohlcv: pd.DataFrame, mark: float) -> None:
        self._ohlcv = ohlcv
        self._mark = mark
        self.calls: list[str] = []

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        self.calls.append(symbol)
        return self._ohlcv

    def mark_price(self, symbol: str) -> float:
        return self._mark


def _ohlcv_df(n: int = 30) -> pd.DataFrame:
    base = 100.0
    rows = []
    ts0 = NOW - timedelta(hours=4 * n)
    for i in range(n):
        c = base + i
        rows.append({
            "timestamp": ts0 + timedelta(hours=4 * i),
            "open": c - 1, "high": c + 2, "low": c - 2, "close": c, "volume": 10.0,
        })
    return pd.DataFrame(rows)


def test_atr_from_ohlcv_is_positive_house_formula() -> None:
    atr = _atr_from_ohlcv(_ohlcv_df(), period=14)
    assert atr > 0
    # constant-range bars (H-L = 4 each step, gaps small) -> ATR near the true range
    assert 3.0 < atr < 6.0


def test_atr_too_few_candles_degrades_to_zero() -> None:
    assert _atr_from_ohlcv(_ohlcv_df(5), period=14) == 0.0


def test_make_paper_price_fn_uses_exchange_mark_and_atr() -> None:
    ex = _FakeExchange(_ohlcv_df(), mark=12345.0)
    price_fn = make_paper_price_fn(ex, timeframe="4h")
    mark, atr = price_fn("BTC")
    assert mark == 12345.0
    assert atr > 0
    assert ex.calls == [symbol_for_coin("BTC")]   # mapped BTC -> BTC/USDT:USDT


def test_paper_price_fn_failure_is_caught_by_assembly(tmp_path: Path) -> None:
    # An exchange that raises on ohlcv must still degrade (not abort) via assemble_evidence.
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_universe(state_dir, 4, ["BTC"])

    class _BoomExchange:
        def ohlcv(self, *a, **k):
            raise RuntimeError("no network")

        def mark_price(self, *a, **k):
            raise RuntimeError("no network")

    price_fn = make_paper_price_fn(_BoomExchange())
    out = build(str(state_dir), str(content_dir), 4, NOW, price_fn=price_fn)
    assert out["BTC"]["price_card"]["mark"] is None
    assert out["BTC"]["price_card"]["note"] == PRICE_CARD_UNAVAILABLE_NOTE
