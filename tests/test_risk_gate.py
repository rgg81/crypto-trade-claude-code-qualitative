"""Unit tests for the ported deterministic risk gate (futures_fund.risk_gate). Pure + offline:
exercises the RR floor (constant + adaptive-tightening), the liq-distance rule, heat headroom,
min-notional, and the flat/breaker hard-stops. No network, no state."""
from __future__ import annotations

from futures_fund.models import (
    MmrBracket,
    PortfolioHealth,
    SymbolSpec,
    TradeProposal,
    mood_to_regime,
)
from futures_fund.risk_gate import (
    MIN_LIQ_DISTANCE_MULT,
    MIN_RR,
    GateInputs,
    evaluate,
)


def _spec() -> SymbolSpec:
    return SymbolSpec(
        symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_notional=5.0,
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12,
                                 mmr=0.004, maint_amount=0.0, max_leverage=125.0)])


def _prop(entry=100.0, stop=96.0, tps=(112.0), direction="long") -> TradeProposal:
    return TradeProposal(symbol="BTCUSDT", direction=direction, entry=entry, stop=stop,
                         take_profits=list(tps) if isinstance(tps, (list, tuple)) else [tps],
                         atr=2.0, confidence=0.7, horizon_hours=4, funding_rate=0.0)


def _health(equity=10_000.0, peak=10_000.0) -> PortfolioHealth:
    return PortfolioHealth(equity=equity, peak_equity=peak)


def _gi(prop=None, *, rr_floor=MIN_RR, open_positions=None, health=None,
        daily=0.0, weekly=0.0, monthly=0.0) -> GateInputs:
    return GateInputs(proposal=prop or _prop(), spec=_spec(),
                      regime=mood_to_regime("greedy", 0.2), health=health or _health(),
                      open_positions=open_positions or [], rr_floor=rr_floor,
                      daily_pnl_pct=daily, weekly_pnl_pct=weekly, monthly_pnl_pct=monthly)


def test_clean_proposal_approves_with_sized_trade():
    d = evaluate(_gi())
    assert d.verdict in ("approve", "resize")
    assert d.sized_trade is not None and d.sized_trade.qty > 0
    assert d.sized_trade.leverage > 0


def test_sub_rr_proposal_is_vetoed():
    # reward 1 / risk 4 = 0.25 << 2.0
    d = evaluate(_gi(_prop(entry=100.0, stop=96.0, tps=[101.0])))
    assert d.verdict == "veto" and "RR" in d.reason and d.sized_trade is None


def test_exactly_2r_is_not_vetoed_by_float_rounding():
    # reward 8 / risk 4 = 2.0 exactly — must clear the floor (the _RR_EPS tolerance)
    d = evaluate(_gi(_prop(entry=100.0, stop=96.0, tps=[108.0])))
    assert d.verdict != "veto"


def test_adaptive_rr_floor_can_only_tighten_above_min_rr():
    # a 2.2-RR trade clears the 2.0 constant but a tightened floor of 2.5 vetoes it
    prop = _prop(entry=100.0, stop=96.0, tps=[108.8])   # RR = 8.8/4 = 2.2
    assert evaluate(_gi(prop, rr_floor=MIN_RR)).verdict != "veto"
    assert evaluate(_gi(prop, rr_floor=2.5)).verdict == "veto"


def test_adaptive_rr_floor_below_min_cannot_weaken_the_guard():
    # a floor BELOW MIN_RR (e.g. 1.6) must never let a sub-2.0 trade through — the gate uses
    # max(MIN_RR, rr_floor). RR = 7/4 = 1.75 < 2.0 -> still vetoed even with rr_floor=1.6.
    d = evaluate(_gi(_prop(entry=100.0, stop=96.0, tps=[107.0]), rr_floor=1.6))
    assert d.verdict == "veto" and "2.00" in d.reason


def test_wide_stop_vetoes_on_liq_distance():
    d = evaluate(_gi(_prop(entry=100.0, stop=55.0, tps=[235.0])))   # RR 3.0, 45% stop gap
    assert d.verdict == "veto" and "liq" in d.reason   # liq-distance rule (leverage cap or final)


def test_no_heat_headroom_vetoes():
    # an open book already at the heat cap leaves no room -> veto
    heavy = [{"qty": 1000.0, "entry": 100.0, "stop": 90.0, "direction": "long"}]
    d = evaluate(_gi(open_positions=heavy))
    assert d.verdict == "veto" and "heat" in d.reason


def test_circuit_breaker_halts_new_entries():
    # a -7% day trips the daily halt-new breaker
    d = evaluate(_gi(daily=-0.07))
    assert d.verdict == "veto" and "circuit breaker" in d.reason


def test_stressed_health_forces_flat():
    # >10% drawdown -> stressed tier -> caps.bias flat -> veto
    d = evaluate(_gi(health=_health(equity=8_500.0, peak=10_000.0)))
    assert d.verdict == "veto" and "risk-off" in d.reason


def test_liq_distance_constant_unchanged():
    assert MIN_LIQ_DISTANCE_MULT == 2.5 and MIN_RR == 2.0
