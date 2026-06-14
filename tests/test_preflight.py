"""Offline tests for scripts/preflight.py — the lean 4h sentiment-desk preflight.

The whole step runs with a FAKE exchange (canned OHLCV/mark/funding/symbol_spec) and an injected
`now`, so NO live network is touched. Coverage:
  * a held position whose latest COMPLETED bar wicked through its STOP is CLOSED, realized PnL hits
    the wallet balance, the position is dropped from positions.json, and the journal decision is
    patched with the Phase-2 outcome;
  * a position whose bar did NOT trigger is CARRIED;
  * context.json carries a per-symbol card (spec + mark + atr + funding_rate +
    funding_interval_hours) for every universe symbol, plus holdings cards and a SCORECARD;
  * pacing is present and keyed to the 1% monthly FLOOR (monthly_target == 0.01).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from futures_fund.journal import append_decision, read_all_decisions
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.state import (
    AccountState,
    Position,
    load_positions,
    save_account,
    save_positions,
)
from scripts.preflight import run_preflight

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)

# Universe of unified ccxt symbols -> their raw exchange ids (what Position.symbol carries).
_RAW = {"BTC/USDT:USDT": "BTCUSDT", "ETH/USDT:USDT": "ETHUSDT"}
_MARK = {"BTC/USDT:USDT": 60000.0, "ETH/USDT:USDT": 3000.0}


def _ohlcv(symbol: str, *, stop_wick: bool) -> pd.DataFrame:
    """30 completed 4h bars + 1 still-FORMING bar (window open == NOW, so last_completed_frame
    drops it). When `stop_wick` the last COMPLETED bar's LOW dives to 56000 — enough to wick through
    a long stop at 58000; otherwise it stays well above any stop."""
    base = _MARK[symbol]
    rows = []
    # Index 30 (the last row) opens its window exactly at NOW -> still FORMING, so
    # last_completed_frame drops it and the last COMPLETED bar is index 29 (where the wick lives).
    ts0 = NOW - timedelta(hours=4 * 30)
    for i in range(31):
        c = base
        low = c * 0.99
        high = c * 1.01
        is_last_completed = (i == 29)   # index 30 is the forming bar (window opens at NOW)
        if is_last_completed and stop_wick and symbol == "BTC/USDT:USDT":
            low = 56000.0               # wicks through the 58000 long stop
        rows.append({
            "timestamp": ts0 + timedelta(hours=4 * i),
            "open": c, "high": high, "low": low, "close": c, "volume": 100.0,
        })
    return pd.DataFrame(rows)


class _FakeFunding:
    def __init__(self, rate: float, interval_hours: float, mark: float) -> None:
        self.current_rate = rate
        self.interval_hours = interval_hours
        self.mark_price = mark


def _spec(raw_id: str) -> SymbolSpec:
    return SymbolSpec(
        symbol=raw_id, tick_size=0.1, step_size=0.001, min_notional=5.0,
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12, mmr=0.05,
                                 maint_amount=0.0, max_leverage=20.0)],
    )


class _FakeExchange:
    """Offline FuturesExchange stand-in keyed by UNIFIED symbol; no network."""

    def __init__(self, *, stop_wick: bool) -> None:
        self._stop_wick = stop_wick
        self.ohlcv_calls: list[str] = []
        self._raw_to_unified = {raw: uni for uni, raw in _RAW.items()}

    def unified_for_raw(self, raw_id: str) -> str | None:
        return self._raw_to_unified.get(raw_id)

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        self.ohlcv_calls.append(symbol)
        return _ohlcv(symbol, stop_wick=self._stop_wick)

    def mark_price(self, symbol: str) -> float:
        return _MARK[symbol]

    def funding(self, symbol: str) -> _FakeFunding:
        return _FakeFunding(rate=0.0001, interval_hours=8.0, mark=_MARK[symbol])

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        return _spec(_RAW[symbol])


def _seed_state(state_dir: Path, *, balance: float = 10_000.0) -> None:
    save_account(str(state_dir), AccountState(balance=balance, peak_equity=balance))
    # BTC long with a stop the wick will hit; ETH long that survives.
    btc = Position(symbol="BTCUSDT", direction="long", qty=0.5, entry=60000.0, stop=58000.0,
                   take_profits=[70000.0], leverage=5.0, margin=6000.0, liq_price=50000.0,
                   opened_cycle=40, opened_ts=NOW - timedelta(hours=24), decision_id="dec-btc")
    eth = Position(symbol="ETHUSDT", direction="long", qty=2.0, entry=3000.0, stop=2700.0,
                   take_profits=[3600.0], leverage=5.0, margin=1200.0, liq_price=2400.0,
                   opened_cycle=41, opened_ts=NOW - timedelta(hours=8), decision_id="dec-eth")
    save_positions(str(state_dir), [btc, eth])


def _seed_journal(memory_dir: Path) -> None:
    append_decision(str(memory_dir), {
        "id": "dec-btc", "ts": NOW - timedelta(hours=24), "cycle": 40, "symbol": "BTCUSDT",
        "direction": "long", "entry": 60000.0, "stop": 58000.0, "size": 0.5,
        "rationale": "crowd euphoria fading, fresh ETF inflows narrative",
        "falsifiable_prediction": "BTC holds 58k and reclaims 62k within 3 bars",
        "confidence": 0.6, "contributing_agents": ["onchain", "social"],
    })
    append_decision(str(memory_dir), {
        "id": "dec-eth", "ts": NOW - timedelta(hours=8), "cycle": 41, "symbol": "ETHUSDT",
        "direction": "long", "entry": 3000.0, "stop": 2700.0, "size": 2.0,
        "rationale": "staking unlock fear overdone",
        "falsifiable_prediction": "ETH bases above 2900 and grinds to 3300",
        "confidence": 0.55, "contributing_agents": ["news"],
    })


def _run(tmp_path: Path, *, stop_wick: bool, cycle: int = 42):
    state_dir, memory_dir = tmp_path / "state", tmp_path / "memory"
    _seed_state(state_dir)
    _seed_journal(memory_dir)
    ex = _FakeExchange(stop_wick=stop_wick)
    ctx = run_preflight(ex, str(state_dir), str(memory_dir), list(_RAW), cycle, NOW)
    return state_dir, memory_dir, ctx


# --------------------------------------------------------------------------- exits close + journal


def test_stop_hit_position_is_closed_and_journaled(tmp_path: Path) -> None:
    state_dir, memory_dir, ctx = _run(tmp_path, stop_wick=True)

    # BTC was closed on the stop; ETH carried.
    closed = ctx["audit"]["closed"]
    assert [c["symbol"] for c in closed] == ["BTCUSDT"]
    assert closed[0]["reason"] == "stop"
    # a long stopped from 60000 -> ~58000 is a LOSS
    assert closed[0]["pnl"] < 0

    # positions.json now holds only ETH (BTC dropped from the book).
    remaining = load_positions(str(state_dir))
    assert [p.symbol for p in remaining] == ["ETHUSDT"]

    # the BTC journal decision was patched with the Phase-2 outcome.
    btc_dec = next(d for d in read_all_decisions(str(memory_dir)) if d["id"] == "dec-btc")
    assert btc_dec["realized_pnl"] is not None
    assert btc_dec["realized_pnl"] < 0
    assert btc_dec["prediction_correct"] is False
    assert btc_dec["exit_ts"] is not None
    # ETH stays open (no Phase-2 outcome).
    eth_dec = next(d for d in read_all_decisions(str(memory_dir)) if d["id"] == "dec-eth")
    assert eth_dec.get("realized_pnl") is None

    # the realized loss hit the wallet balance.
    acct = json.loads((state_dir / "account.json").read_text())
    assert acct["balance"] < 10_000.0


def test_no_trigger_carries_all_positions(tmp_path: Path) -> None:
    state_dir, memory_dir, ctx = _run(tmp_path, stop_wick=False)
    assert ctx["audit"]["closed"] == []
    remaining = load_positions(str(state_dir))
    assert {p.symbol for p in remaining} == {"BTCUSDT", "ETHUSDT"}
    # both decisions stay open (Phase-2 outcome unset).
    assert all(d.get("realized_pnl") is None for d in read_all_decisions(str(memory_dir)))


# ----------------------------------------------------------------- per-symbol market-data cards


def test_context_carries_spec_mark_atr_funding_per_symbol(tmp_path: Path) -> None:
    _, _, ctx = _run(tmp_path, stop_wick=False)

    assert set(ctx["symbols"]) == set(_RAW)
    for sym, raw in _RAW.items():
        card = ctx["symbols"][sym]
        assert card["spec"]["symbol"] == raw           # SymbolSpec serialized in full
        assert card["spec"]["tick_size"] == 0.1
        assert card["spec"]["min_notional"] == 5.0
        assert card["mark"] == _MARK[sym]              # mark from the fake exchange
        assert card["atr"] is not None and card["atr"] > 0   # house ATR computed
        assert card["funding_rate"] == 0.0001          # current funding rate
        assert card["funding_interval_hours"] == 8.0   # per-symbol interval


def test_context_carries_holdings_cards_with_thesis(tmp_path: Path) -> None:
    # no trigger: both positions survive and get review cards.
    _, _, ctx = _run(tmp_path, stop_wick=False)
    holdings = {h["symbol"]: h for h in ctx["holdings"]}
    assert set(holdings) == {"BTCUSDT", "ETHUSDT"}
    btc = holdings["BTCUSDT"]
    assert btc["entry"] == 60000.0
    assert btc["mark"] == 60000.0
    assert btc["r_progress"] is not None
    assert btc["dist_to_stop_pct"] is not None
    assert btc["bars_held"] == 6.0                     # 24h / 4h
    # original thesis + falsifiable prediction pulled from the journal decision.
    assert "euphoria" in btc["original_thesis"]
    assert btc["falsifiable_prediction"].startswith("BTC holds 58k")


# ----------------------------------------------------------------------- pacing keyed to 1% floor


def test_pacing_directive_keyed_to_one_percent_floor(tmp_path: Path) -> None:
    _, _, ctx = _run(tmp_path, stop_wick=False)
    pacing = ctx["pacing"]
    assert pacing["monthly_target"] == 0.01            # 1%/mo FLOOR, not the legacy 5%
    assert pacing["mode"] in {"soft", "normal", "press", "throttle"}
    assert pacing["directive"]                         # a human directive is present
    assert "suggested_risk_mult" in pacing


def test_scorecard_present_with_health_and_metrics(tmp_path: Path) -> None:
    _, _, ctx = _run(tmp_path, stop_wick=False)
    sc = ctx["scorecard"]
    assert ctx["health_tier"] in {"healthy", "caution", "stressed"}
    assert sc["health_tier"] == ctx["health_tier"]
    assert "sharpe" in sc and "hit_rate" in sc and "profit_factor" in sc
    assert "per_expert_hit_rate" in sc
    for key in ("daily_pnl_pct", "weekly_pnl_pct", "monthly_pnl_pct"):
        assert key in ctx and key in sc


def test_context_written_to_cycle_dir(tmp_path: Path) -> None:
    state_dir, _, ctx = _run(tmp_path, stop_wick=False, cycle=7)
    written = json.loads((state_dir / "cycle" / "7" / "context.json").read_text())
    assert written["cycle"] == 7
    assert set(written["symbols"]) == set(_RAW)
    assert written["pacing"]["monthly_target"] == 0.01
