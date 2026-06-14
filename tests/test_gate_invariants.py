"""Adversarial-review regression tests for scripts/gate_execute_cli.gate_execute.

Four defects the existing wiring tests masked (they hand-build a context shape the real
preflight never emits). Everything here is OFFLINE: injected context/proposals/auditor on
disk, a fake exchange, and an injected `now`.

  1. DOUBLE-OPEN, same symbol+direction: two AgentProposals for BTCUSDT long must NOT both
     open. The gate must collapse the approved set to one trade per symbol.
  2. CONFLICTING DIRECTIONS, same symbol: a BTCUSDT long AND a BTCUSDT short must never both
     open (a fully hedged, fee/funding-bleeding book).
  3. PRODUCER/CONSUMER KEY MISMATCH on context['symbols']: the REAL preflight keys cards by
     the UNIFIED ccxt symbol (BTC/USDT:USDT); the gate must still resolve the per-symbol
     spec/mark for a proposal carrying the RAW id (BTCUSDT). An end-to-end preflight->gate
     pipe locks the contract.
  4. PRODUCER/CONSUMER KEY MISMATCH on the loss-breaker inputs: the REAL preflight emits
     TOP-LEVEL daily_pnl_pct/weekly_pnl_pct/monthly_pnl_pct; the gate must feed those into
     the circuit-breaker so a -6% day / -12% week / -20% month HALTS new entries.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from futures_fund.config import Settings
from futures_fund.contracts import AgentProposal
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.journal import append_decision, read_all_decisions
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.state import (
    AccountState,
    Position,
    load_positions,
    save_account,
    save_positions,
)
from scripts.gate_execute_cli import gate_execute
from scripts.preflight import run_preflight

NOW = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def _spec(symbol: str = "BTCUSDT") -> dict:
    return SymbolSpec(
        symbol=symbol, tick_size=0.01, step_size=0.001, min_notional=5.0,
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12,
                                 mmr=0.004, maint_amount=0.0, max_leverage=125.0)],
    ).model_dump()


def _proposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=96.0,
              tps=None, atr=2.0, confidence=0.7) -> dict:
    ap = AgentProposal(
        symbol=symbol, direction=direction, entry=entry, stop=stop,
        take_profits=tps if tps is not None else [entry + 12.0],
        atr=atr, confidence=confidence, rationale="strong bullish flow",
        falsifiable_prediction="BTC reclaims 105 within 8 candles",
    ).model_dump()
    ap["contributing_agents"] = ["flow", "narrative"]
    return ap


def _short_proposal(symbol="BTCUSDT", entry=100.0, stop=104.0, confidence=0.7) -> dict:
    # mirror long geometry: RR = (entry-tp)/(stop-entry) = (100-88)/(104-100) = 3.0
    ap = AgentProposal(
        symbol=symbol, direction="short", entry=entry, stop=stop,
        take_profits=[entry - 12.0], atr=2.0, confidence=confidence,
        rationale="bearish capitulation flow",
        falsifiable_prediction="BTC loses 95 within 8 candles",
    ).model_dump()
    ap["contributing_agents"] = ["flow"]
    return ap


def _ctx_raw_keyed(symbol="BTCUSDT", mark=100.0, atr=2.0, funding=0.0,
                   mood="greedy", dispersion=0.2) -> dict:
    """The legacy hand-built context shape (raw-keyed symbols + pnl block) used by the
    dedup tests where the key mismatch is not under test."""
    return {
        "crowd_mood": {"mood": mood, "dispersion": dispersion},
        "symbols": {symbol: {"spec": _spec(symbol), "mark": mark, "atr": atr,
                             "funding_rate": funding}},
        "pnl": {"daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0},
        "scorecard": {"recent_hit_rate": 0.55},
    }


def _seed(state_dir, cycle, *, context, proposals, audit_passed=True) -> None:
    save_output(state_dir, cycle, "context", context)
    save_output(state_dir, cycle, "proposals", {
        "proposals": proposals, "management": [], "triggers": [], "cancel_triggers": []})
    save_output(state_dir, cycle, "auditor",
                {"passed": audit_passed, "checks": [], "mismatches": []})


def _run(state_dir, memory_dir, cycle):
    return gate_execute(str(state_dir), str(memory_dir), cycle, NOW,
                        settings=_settings(), exchange=None)


# --------------------------------------------------------------------------- #
# 1. DOUBLE-OPEN: two proposals, same symbol + same direction                   #
# --------------------------------------------------------------------------- #
def test_duplicate_symbol_same_direction_opens_only_one(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, context=_ctx_raw_keyed(),
          proposals=[_proposal(confidence=0.6), _proposal(confidence=0.9)])
    report = _run(state_dir, memory_dir, 1)

    # exactly ONE BTCUSDT long opens — the desk's one-open-per-symbol invariant.
    assert len(report["opened"]) == 1
    positions = load_positions(state_dir)
    assert len(positions) == 1
    assert positions[0].symbol == "BTCUSDT" and positions[0].direction == "long"

    # exactly ONE journal decision; the open is linked to it (no shared decision_id).
    decisions = read_all_decisions(memory_dir)
    assert len(decisions) == 1
    assert report["opened"][0]["decision_id"] == positions[0].decision_id


# --------------------------------------------------------------------------- #
# 2. CONFLICTING DIRECTIONS: long + short for the same symbol                    #
# --------------------------------------------------------------------------- #
def test_conflicting_directions_never_both_open(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, context=_ctx_raw_keyed(),
          proposals=[_proposal(), _short_proposal()])
    report = _run(state_dir, memory_dir, 1)

    positions = load_positions(state_dir)
    dirs = {(p.symbol, p.direction) for p in positions}
    # never a simultaneous long AND short on one coin.
    assert not ({("BTCUSDT", "long"), ("BTCUSDT", "short")} <= dirs)
    assert len(positions) <= 1
    assert len(report["opened"]) <= 1


# --------------------------------------------------------------------------- #
# 3 + 4. END-TO-END: the REAL preflight context feeds the REAL gate             #
# --------------------------------------------------------------------------- #
_RAW = {"BTC/USDT:USDT": "BTCUSDT"}
_MARK = {"BTC/USDT:USDT": 100.0}


def _ohlcv(symbol: str) -> pd.DataFrame:
    base = _MARK[symbol]
    rows = []
    ts0 = NOW - timedelta(hours=4 * 30)
    for i in range(31):
        rows.append({"timestamp": ts0 + timedelta(hours=4 * i),
                     "open": base, "high": base * 1.01, "low": base * 0.99,
                     "close": base, "volume": 100.0})
    return pd.DataFrame(rows)


class _FakeFunding:
    def __init__(self, rate, interval_hours):
        self.current_rate = rate
        self.interval_hours = interval_hours


class _FakeExchange:
    """Offline FuturesExchange stand-in keyed by UNIFIED symbol (what the real one speaks)."""

    def __init__(self):
        self._raw_to_unified = {raw: uni for uni, raw in _RAW.items()}

    def unified_for_raw(self, raw_id):
        return self._raw_to_unified.get(raw_id)

    def ohlcv(self, symbol, timeframe="4h", limit=500):
        return _ohlcv(symbol)

    def mark_price(self, symbol):
        return _MARK[symbol]

    def funding(self, symbol):
        return _FakeFunding(rate=0.0, interval_hours=8.0)

    def symbol_spec(self, symbol):
        # the REAL preflight emits the RAW id inside the spec blob (BTCUSDT), keyed by UNIFIED.
        return SymbolSpec.model_validate(_spec(_RAW[symbol]))


def _build_real_context(tmp_path, *, balance=10_000.0):
    """Run the REAL preflight to produce a context.json with UNIFIED-keyed symbol cards and
    TOP-LEVEL *_pnl_pct keys — the exact shape the gate must consume."""
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    save_account(str(state_dir), AccountState(balance=balance, peak_equity=balance))
    save_positions(str(state_dir), [])
    ex = _FakeExchange()
    ctx = run_preflight(ex, str(state_dir), str(memory_dir), list(_RAW), 1, NOW,
                        default_balance=balance)
    return state_dir, memory_dir, ctx


def test_real_preflight_context_lets_gate_open(tmp_path):
    """Finding 3: the gate must resolve the per-symbol spec/mark from the REAL preflight's
    UNIFIED-keyed symbols dict, given a proposal carrying the RAW id."""
    state_dir, memory_dir, ctx = _build_real_context(tmp_path)

    # ground-truth shape checks: cards keyed by unified, raw id only inside spec.symbol.
    assert "BTC/USDT:USDT" in ctx["symbols"]
    assert "BTCUSDT" not in ctx["symbols"]
    assert ctx["symbols"]["BTC/USDT:USDT"]["spec"]["symbol"] == "BTCUSDT"
    assert "pnl" not in ctx  # the real preflight never writes a pnl block

    # add crowd_mood (preflight does not, but the gate tolerates its absence) and seed proposal.
    ctx["crowd_mood"] = {"mood": "greedy", "dispersion": 0.2}
    save_output(str(state_dir), 1, "context", ctx)
    save_output(str(state_dir), 1, "proposals", {
        "proposals": [_proposal()], "management": [],
        "triggers": [], "cancel_triggers": []})
    save_output(str(state_dir), 1, "auditor", {"passed": True, "checks": [], "mismatches": []})

    report = _run(state_dir, memory_dir, 1)

    # the proposal must OPEN — not be vetoed "no spec in context.json".
    assert report["vetoed"] == [], report["vetoed"]
    assert len(report["opened"]) == 1
    assert report["opened"][0]["symbol"] == "BTCUSDT"
    assert len(load_positions(str(state_dir))) == 1


def test_loss_breaker_fires_off_real_preflight_pnl_keys(tmp_path):
    """Finding 4: a -7% day in the REAL preflight's TOP-LEVEL daily_pnl_pct must HALT new
    entries via the circuit breaker — the gate must read those keys, not a nonexistent
    context['pnl'] block."""
    state_dir, memory_dir, ctx = _build_real_context(tmp_path)

    # Inject a daily loss past the -6% halt-new limb at the REAL top-level key.
    ctx["daily_pnl_pct"] = -0.07
    ctx["scorecard"]["daily_pnl_pct"] = -0.07
    ctx["crowd_mood"] = {"mood": "greedy", "dispersion": 0.2}
    save_output(str(state_dir), 1, "context", ctx)
    save_output(str(state_dir), 1, "proposals", {
        "proposals": [_proposal()], "management": [],
        "triggers": [], "cancel_triggers": []})
    save_output(str(state_dir), 1, "auditor", {"passed": True, "checks": [], "mismatches": []})

    report = _run(state_dir, memory_dir, 1)

    # the breaker halts new entries: nothing opens, the proposal is vetoed for the halt.
    assert report["opened"] == []
    assert load_positions(str(state_dir)) == []
    assert len(report["vetoed"]) == 1
    assert "halt" in report["vetoed"][0]["reason"].lower() \
        or "circuit" in report["vetoed"][0]["reason"].lower() \
        or "daily" in report["vetoed"][0]["reason"].lower()


def test_weekly_loss_breaker_fires_off_real_preflight_pnl_keys(tmp_path):
    """Finding 4 (weekly limb): -13% week at the top-level weekly_pnl_pct halts new entries."""
    state_dir, memory_dir, ctx = _build_real_context(tmp_path)
    ctx["weekly_pnl_pct"] = -0.13
    ctx["crowd_mood"] = {"mood": "greedy", "dispersion": 0.2}
    save_output(str(state_dir), 1, "context", ctx)
    save_output(str(state_dir), 1, "proposals", {
        "proposals": [_proposal()], "management": [],
        "triggers": [], "cancel_triggers": []})
    save_output(str(state_dir), 1, "auditor", {"passed": True, "checks": [], "mismatches": []})

    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == []
    assert load_positions(str(state_dir)) == []


# --------------------------------------------------------------------------- #
# Finding: the gate reads the preflight scorecard's HIT-RATE key               #
# The REAL preflight scorecard writes `hit_rate` (not `recent_hit_rate`); the  #
# gate must feed that into PortfolioHealth.recent_hit_rate, not the 0.5 default.#
# --------------------------------------------------------------------------- #
def test_preflight_hit_rate_reaches_gate_portfolio_health(tmp_path, monkeypatch):
    import scripts.gate_execute_cli as gx

    # capture every PortfolioHealth the gate builds.
    seen: list[float] = []
    real_ph = gx.PortfolioHealth

    def _spy(*args, **kwargs):
        if "recent_hit_rate" in kwargs:
            seen.append(kwargs["recent_hit_rate"])
        return real_ph(*args, **kwargs)

    monkeypatch.setattr(gx, "PortfolioHealth", _spy)

    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    # A REAL-shaped scorecard: hit_rate present, recent_hit_rate ABSENT (the preflight's key).
    ctx = _ctx_raw_keyed()
    ctx["scorecard"] = {"hit_rate": 0.7}
    _seed(state_dir, 1, context=ctx, proposals=[_proposal()])
    _run(state_dir, memory_dir, 1)

    assert seen, "the gate must build a PortfolioHealth"
    assert seen[0] == 0.7, f"expected the preflight hit_rate 0.7 to reach the gate, got {seen[0]}"


def test_gate_still_reads_legacy_recent_hit_rate_key(tmp_path, monkeypatch):
    # back-compat: a hand-built context carrying only recent_hit_rate still flows through.
    import scripts.gate_execute_cli as gx

    seen: list[float] = []
    real_ph = gx.PortfolioHealth

    def _spy(*a, **k):
        seen.append(k.get("recent_hit_rate"))
        return real_ph(*a, **k)

    monkeypatch.setattr(gx, "PortfolioHealth", _spy)

    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ctx = _ctx_raw_keyed()
    ctx["scorecard"] = {"recent_hit_rate": 0.62}
    _seed(state_dir, 1, context=ctx, proposals=[_proposal()])
    _run(state_dir, memory_dir, 1)
    assert seen and seen[0] == 0.62
