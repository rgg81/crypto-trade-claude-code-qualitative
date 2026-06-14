import pytest

from futures_fund.models import PortfolioHealth, RegimeState
from futures_fund.policy import caps_for, circuit_breaker, cvar


def _health(equity, peak):
    return PortfolioHealth(equity=equity, peak_equity=peak)


# --- caps_for: MEDIUM-AGGRESSIVE base caps -------------------------------------

def test_healthy_low_vol_trend_is_full_caps():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(10_000, 10_000))
    assert caps.max_leverage == 8.0
    assert caps.per_trade_risk_pct == pytest.approx(0.020)
    assert caps.max_heat == pytest.approx(0.18)
    assert caps.bias == "normal"


def test_healthy_high_vol_trend_caps():
    caps = caps_for(RegimeState(quadrant="high_vol_trend"), _health(10_000, 10_000))
    assert caps.max_leverage == 6.0
    assert caps.per_trade_risk_pct == pytest.approx(0.015)
    assert caps.max_heat == pytest.approx(0.14)


def test_healthy_low_vol_range_caps():
    caps = caps_for(RegimeState(quadrant="low_vol_range"), _health(10_000, 10_000))
    assert caps.max_leverage == 5.0
    assert caps.per_trade_risk_pct == pytest.approx(0.015)
    assert caps.max_heat == pytest.approx(0.12)


def test_high_vol_range_is_reduced():
    caps = caps_for(RegimeState(quadrant="high_vol_range"), _health(10_000, 10_000))
    assert caps.max_leverage == 3.0
    assert caps.per_trade_risk_pct == pytest.approx(0.010)
    assert caps.max_heat == pytest.approx(0.07)


def test_transition_caps_and_reduce_bias():
    caps = caps_for(RegimeState(quadrant="transition"), _health(10_000, 10_000))
    assert caps.max_leverage == 3.0
    assert caps.per_trade_risk_pct == pytest.approx(0.010)
    assert caps.max_heat == pytest.approx(0.06)
    assert caps.bias == "reduce"


def test_caution_halves_caps():
    # equity 9400/10000 -> dd 6% -> caution tier; halve the healthy Q1 caps
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(9_400, 10_000))
    assert caps.max_leverage == pytest.approx(4.0)
    assert caps.per_trade_risk_pct == pytest.approx(0.010)
    assert caps.max_heat == pytest.approx(0.09)
    assert caps.bias == "reduce"


def test_stressed_forces_flat_bias_and_zero_risk():
    caps = caps_for(RegimeState(quadrant="low_vol_trend"), _health(8_500, 10_000))  # dd 15%
    assert caps.bias == "flat"
    assert caps.per_trade_risk_pct == 0.0
    assert caps.max_leverage == 1.0
    assert caps.max_heat == 0.0


def test_transition_regime_minimum_size():
    caps = caps_for(RegimeState(quadrant="transition"), _health(10_000, 10_000))
    assert caps.bias == "reduce"
    assert caps.max_leverage <= 3.0


# --- circuit_breaker: aggression-tuned thresholds ------------------------------

def test_circuit_breaker_no_action_when_calm():
    state = circuit_breaker(daily_pnl_pct=0.01, weekly_pnl_pct=0.02, monthly_pnl_pct=0.05,
                            dd_from_peak=0.02)
    assert state.allow_new_entries is True
    assert state.force_flatten is False
    assert state.risk_multiplier == pytest.approx(1.0)


def test_circuit_breaker_step_down_halves_at_5pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                            dd_from_peak=0.06)
    assert state.risk_multiplier == pytest.approx(0.5)
    assert state.force_flatten is False


def test_circuit_breaker_deeper_step_down_at_12pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.01, weekly_pnl_pct=-0.02, monthly_pnl_pct=-0.03,
                            dd_from_peak=0.13)
    assert state.risk_multiplier == pytest.approx(0.35)
    assert state.force_flatten is False


def test_circuit_breaker_daily_loss_halts_new_at_6pct():
    state = circuit_breaker(daily_pnl_pct=-0.065, weekly_pnl_pct=-0.01, monthly_pnl_pct=-0.02,
                            dd_from_peak=0.04)
    assert state.allow_new_entries is False
    assert state.force_flatten is False


def test_circuit_breaker_daily_loss_above_6pct_does_not_halt():
    state = circuit_breaker(daily_pnl_pct=-0.05, weekly_pnl_pct=-0.01, monthly_pnl_pct=-0.02,
                            dd_from_peak=0.02)
    assert state.allow_new_entries is True


def test_circuit_breaker_weekly_loss_halts_new_at_12pct():
    state = circuit_breaker(daily_pnl_pct=-0.02, weekly_pnl_pct=-0.13, monthly_pnl_pct=-0.05,
                            dd_from_peak=0.10)
    assert state.allow_new_entries is False
    assert state.force_flatten is False


def test_circuit_breaker_monthly_loss_halts_new_at_20pct_no_flatten():
    # monthly <= -20% halts new entries but is NOT the kill-switch on its own
    state = circuit_breaker(daily_pnl_pct=-0.02, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.21,
                            dd_from_peak=0.10)
    assert state.allow_new_entries is False
    assert state.force_flatten is False


def test_circuit_breaker_no_force_flatten_at_15pct_drawdown():
    # dd 15% is a deep step-down zone but must NOT trigger the kill-switch
    state = circuit_breaker(daily_pnl_pct=-0.02, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.16,
                            dd_from_peak=0.15)
    assert state.force_flatten is False
    assert state.risk_multiplier == pytest.approx(0.35)


def test_circuit_breaker_force_flatten_at_22pct_drawdown():
    state = circuit_breaker(daily_pnl_pct=-0.02, weekly_pnl_pct=-0.05, monthly_pnl_pct=-0.10,
                            dd_from_peak=0.22)
    assert state.force_flatten is True
    assert state.allow_new_entries is False
    assert state.risk_multiplier == pytest.approx(0.35)


def test_cvar_is_mean_of_worst_tail():
    # returns; 5% tail of 20 obs = worst 1 obs = -0.10
    returns = [-0.10] + [0.01] * 19
    assert cvar(returns, alpha=0.05) == pytest.approx(-0.10)
