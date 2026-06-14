import pytest

from futures_fund.metrics import (
    agent_attribution,
    calmar,
    hit_rate,
    max_drawdown,
    profit_factor,
    sharpe,
    sortino,
    trial_sharpe_std,
)


def test_trial_sharpe_std_needs_two_qualifying_trials():
    s1 = [0.02, 0.01, 0.02, 0.01, 0.02, 0.01]   # positive per-period Sharpe
    s2 = [-0.01, 0.0, -0.01, 0.0, -0.01, 0.0]   # negative per-period Sharpe
    sig = trial_sharpe_std([s1, s2])
    assert sig is not None and sig > 0          # dispersion across the two trial Sharpes
    # fewer than 2 trials with >= min_obs observations -> None (caller falls back to sr_std)
    assert trial_sharpe_std([s1, [0.01, 0.01]]) is None
    assert trial_sharpe_std([]) is None


def test_sharpe_zero_for_constant_returns():
    assert sharpe([0.01, 0.01, 0.01]) == 0.0
    assert sharpe([]) == 0.0


def test_sharpe_positive_for_positive_mean():
    assert sharpe([0.0, 0.02, 0.01, 0.015]) > 0


def test_sortino_only_penalizes_downside():
    # no negative returns -> sortino is large/inf-guarded but > sharpe-ish; just assert positive
    assert sortino([0.01, 0.02, 0.0]) > 0
    assert sortino([0.01, 0.01]) >= 0


def test_sortino_finite_with_single_or_equal_losses():
    import math
    ppy = 2190  # 4h bars/year
    # ONE loss must NOT be infinite (old bug: std of a 1-element subset -> 0 -> inf).
    s1 = sortino([0.01, 0.02, -0.05, 0.03], periods_per_year=ppy)
    assert math.isfinite(s1)
    assert s1 == pytest.approx(0.0025 / 0.025 * math.sqrt(ppy), rel=1e-6)  # dd=sqrt(.05^2/4)=.025
    # THREE EQUAL losses must NOT be infinite (old bug: std of equal negatives -> 0 -> inf).
    s2 = sortino([0.03, -0.01, -0.01, -0.01, 0.04], periods_per_year=ppy)
    dd = math.sqrt(3 * 0.01 ** 2 / 5)
    assert math.isfinite(s2)
    assert s2 == pytest.approx(0.008 / dd * math.sqrt(ppy), rel=1e-6)
    # A genuinely loss-free, net-positive series is still +inf (no measurable downside).
    assert sortino([0.01, 0.02, 0.0]) == float("inf")


def test_max_drawdown_peak_to_trough():
    assert max_drawdown([100, 110, 90, 95, 120]) == pytest.approx((110 - 90) / 110)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100, 101, 102]) == 0.0


def test_calmar_is_return_over_drawdown():
    assert calmar(annual_return=0.40, mdd=0.10) == pytest.approx(4.0)
    assert calmar(0.40, 0.0) == 0.0  # guard


def test_hit_rate_and_profit_factor():
    closed = [{"realized_pnl": 5.0}, {"realized_pnl": -3.0}, {"realized_pnl": 2.0}]
    assert hit_rate(closed) == pytest.approx(2 / 3)
    assert profit_factor(closed) == pytest.approx(7.0 / 3.0)


def test_profit_factor_no_losses_returns_inf_guard():
    assert profit_factor([{"realized_pnl": 5.0}]) == float("inf")
    assert hit_rate([]) == 0.0


def test_agent_attribution_sums_pnl_and_hit_rate_per_agent():
    closed = [
        {"realized_pnl": 10.0, "contributing_agents": ["research_manager", "trader"]},
        {"realized_pnl": -4.0, "contributing_agents": ["research_manager", "trader"]},
        {"realized_pnl": 6.0, "contributing_agents": ["baseline"]},
    ]
    attr = agent_attribution(closed)
    assert attr["research_manager"]["pnl"] == pytest.approx(6.0)
    assert attr["research_manager"]["count"] == 2
    assert attr["research_manager"]["hit_rate"] == pytest.approx(0.5)
    assert attr["baseline"]["pnl"] == pytest.approx(6.0)


def test_realized_total_classifies_trimmed_winner_with_subzero_runner_as_win():
    # cy78 review [0]: a trade that banked a winning slice (partial_banks) but whose RUNNER closed
    # slightly negative must count as a WIN with its TOTAL realized — not a loss on the runner.
    trimmed = {"symbol": "SOLUSDT", "direction": "short", "entry": 80.0, "stop": 82.0, "size": 10.0,
               "realized_pnl": -5.0, "partial_banks": [{"pnl": 119.45, "cycle": 22}]}  # tot +114.45
    plain_loss = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 95.0,
                  "size": 1.0, "realized_pnl": -10.0}
    closed = [trimmed, plain_loss]
    assert hit_rate(closed) == 0.5                         # trimmed is a WIN, not a loss
    # profit_factor: gains 114.45 / losses 10.0
    assert profit_factor(closed) == pytest.approx(114.45 / 10.0)
    att = agent_attribution([{**trimmed, "contributing_agents": ["derivatives"]}])
    assert att["derivatives"]["pnl"] == pytest.approx(114.45) and att["derivatives"]["wins"] == 1
