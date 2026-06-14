import pytest

from futures_fund.consolidation import cluster_scale, consolidate, cvar_risk_multiplier
from futures_fund.models import CostEstimate, SizedTrade, TradeProposal
from futures_fund.portfolio_risk import position_risk


def _sized(symbol, qty, entry=100.0, stop=95.0, direction="long"):
    prop = TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * 1.2], atr=2.0, confidence=0.6,
                         horizon_hours=4, funding_rate=0.0)
    return SizedTrade(proposal=prop, qty=qty, notional=entry * qty, leverage=5.0,
                      margin=entry * qty / 5.0, liq_price=82.0, cost=CostEstimate())


def test_position_risk_is_downside_only_for_profit_locked_stops():
    eq = 10_000.0
    # long with a normal loss-side stop: risk is the stop distance
    assert position_risk(40.0, 100.0, 95.0, eq, "long") == pytest.approx(0.02)
    # long with a PROFIT-LOCKED stop (above entry): zero downside risk
    assert position_risk(40.0, 100.0, 110.0, eq, "long") == 0.0
    # short symmetric: stop below entry = profit-locked = zero
    assert position_risk(40.0, 100.0, 90.0, eq, "short") == 0.0
    assert position_risk(40.0, 100.0, 105.0, eq, "short") == pytest.approx(0.02)
    # no direction -> legacy absolute distance (back-compat)
    assert position_risk(40.0, 100.0, 110.0, eq) == pytest.approx(0.04)


def _heat(trades, eq):
    return sum(position_risk(t.qty, t.proposal.entry, t.proposal.stop, eq) for t in trades)


def test_cluster_scale_trims_correlated_same_direction_cluster():
    # 3 perfectly-correlated longs, each 2% risk (qty 40, |100-95|=5 -> 40*5/10000=2%) -> 6%.
    eq = 10_000.0
    trades = [_sized(s, 40.0) for s in ("AAA", "BBB", "CCC")]
    corr = {("AAA", "BBB"): 1.0, ("AAA", "CCC"): 1.0, ("BBB", "CCC"): 1.0}
    out = cluster_scale(trades, held=[], equity=eq, corr=corr, cluster_cap=0.03)  # cap 3%
    assert _heat(out, eq) == pytest.approx(0.03, abs=1e-6)  # cluster trimmed to the 3% cap


def test_cluster_scale_leaves_uncorrelated_alone():
    eq = 10_000.0
    trades = [_sized(s, 40.0) for s in ("AAA", "BBB", "CCC")]  # 2% each, 6% total
    out = cluster_scale(trades, held=[], equity=eq, corr={}, cluster_cap=0.03)  # all uncorrelated
    assert _heat(out, eq) == pytest.approx(0.06, abs=1e-6)  # each is its own cluster < cap


def test_cluster_scale_reserves_held_heat_in_the_cluster():
    # A held long (2%) correlated with a new long (2%); cap 3% -> new must trim to 1%.
    eq = 10_000.0
    held = [{"symbol": "AAA", "direction": "long", "qty": 40.0, "entry": 100.0, "stop": 95.0}]
    new = [_sized("BBB", 40.0)]
    out = cluster_scale(new, held=held, equity=eq, corr={("AAA", "BBB"): 0.9}, cluster_cap=0.03)
    assert _heat(out, eq) == pytest.approx(0.01, abs=1e-6)  # 3% cap - 2% held = 1% for the new


def test_cluster_scale_ignores_opposite_direction():
    # A held long and a new short are NOT one cluster even if correlated -> no trim.
    eq = 10_000.0
    held = [{"symbol": "AAA", "direction": "long", "qty": 40.0, "entry": 100.0, "stop": 95.0}]
    new = [_sized("BBB", 40.0, direction="short", stop=105.0)]
    out = cluster_scale(new, held=held, equity=eq, corr={("AAA", "BBB"): 0.99}, cluster_cap=0.03)
    assert _heat(out, eq) == pytest.approx(0.02, abs=1e-6)  # untouched (different direction)


def test_cvar_multiplier_derisks_on_bad_tail():
    calm = cvar_risk_multiplier([0.01, 0.0, -0.01, 0.005], threshold=-0.05, floor=0.5)
    bad = cvar_risk_multiplier([-0.10, -0.08, 0.01, 0.0], threshold=-0.05, floor=0.5)
    assert calm == 1.0
    assert bad == 0.5


def test_cvar_multiplier_no_history_is_one():
    assert cvar_risk_multiplier([], threshold=-0.05) == 1.0


def test_consolidate_scales_book_to_gross_heat_cap():
    # two trades each risking 1% (qty 20, gap 5 on 10k); cap 0.015 -> must scale to 0.75x
    trades = [_sized("BTCUSDT", 20.0), _sized("ETHUSDT", 20.0)]
    out = consolidate(trades, equity=10_000.0, max_heat=0.015)
    total_risk = sum(t.qty * 5.0 / 10_000.0 for t in out)
    assert total_risk == pytest.approx(0.015, abs=1e-9)
    # qty scaled down proportionally
    assert out[0].qty == pytest.approx(20.0 * 0.75)


def test_consolidate_under_cap_is_unchanged():
    trades = [_sized("BTCUSDT", 10.0)]  # 0.5% risk, cap 10%
    out = consolidate(trades, equity=10_000.0, max_heat=0.10)
    assert out[0].qty == 10.0


def test_consolidate_applies_cvar_multiplier():
    trades = [_sized("BTCUSDT", 10.0)]
    out = consolidate(trades, equity=10_000.0, max_heat=0.10, cvar_mult=0.5)
    assert out[0].qty == pytest.approx(5.0)


def test_consolidate_drops_dust():
    trades = [_sized("BTCUSDT", 0.001)]  # negligible risk
    out = consolidate(trades, equity=10_000.0, max_heat=0.10, min_risk_frac=0.001)
    assert out == []
