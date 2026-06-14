import pytest

from futures_fund.portfolio_risk import cluster_heat, portfolio_heat, position_risk


def test_position_risk_fraction():
    # qty 20, stop gap 5 -> 100 USDT risk on 10k equity = 1%
    assert position_risk(qty=20.0, entry=100.0, stop=95.0, equity=10_000.0) == pytest.approx(0.01)


def test_portfolio_heat_sums_positions():
    positions = [
        dict(qty=20.0, entry=100.0, stop=95.0),    # 1%
        dict(qty=10.0, entry=200.0, stop=190.0),   # 100 USDT -> 1%
    ]
    assert portfolio_heat(positions, equity=10_000.0) == pytest.approx(0.02)


def test_cluster_heat_groups_correlated_same_direction():
    # Two long positions correlated >= 0.7 form one cluster; their risks add within it.
    positions = [
        dict(symbol="ETHUSDT", direction="long", qty=20.0, entry=100.0, stop=95.0),  # 1%
        dict(symbol="SOLUSDT", direction="long", qty=10.0, entry=200.0, stop=190.0), # 1%
    ]
    corr = {("ETHUSDT", "SOLUSDT"): 0.8}
    clusters = cluster_heat(positions, equity=10_000.0, corr=corr, threshold=0.7)
    assert max(clusters.values()) == pytest.approx(0.02)  # combined cluster heat


def test_cluster_heat_opposite_directions_not_grouped():
    positions = [
        dict(symbol="ETHUSDT", direction="long", qty=20.0, entry=100.0, stop=95.0),
        dict(symbol="SOLUSDT", direction="short", qty=10.0, entry=200.0, stop=210.0),
    ]
    corr = {("ETHUSDT", "SOLUSDT"): 0.8}
    clusters = cluster_heat(positions, equity=10_000.0, corr=corr, threshold=0.7)
    assert max(clusters.values()) == pytest.approx(0.01)  # separate clusters, each 1%
