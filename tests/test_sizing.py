import pytest

from futures_fund.sizing import choose_leverage, liq_distance_ratio, qty_from_risk


def test_qty_from_risk_basic():
    # risk 1% of 10k = 100 USDT; stop distance 5 -> qty = 20
    result = qty_from_risk(equity=10_000.0, risk_pct=0.01, entry=100.0, stop=95.0)
    assert result == pytest.approx(20.0)


def test_qty_zero_when_no_stop_distance():
    assert qty_from_risk(10_000.0, 0.01, entry=100.0, stop=100.0) == 0.0


def test_liq_distance_ratio_is_liq_gap_over_stop_gap():
    # stop gap = 5; if liq is 12.5 below entry, ratio = 2.5
    ratio = liq_distance_ratio(entry=100.0, stop=95.0, liq_price=87.5, direction="long")
    assert ratio == pytest.approx(2.5)


def test_choose_leverage_respects_cap_and_liq_distance():
    # With mmr 0.004, find max leverage (<=cap 5) keeping liq >= 2.5x stop distance.
    lev = choose_leverage(
        entry=100.0, stop=95.0, qty=20.0, direction="long",
        mmr=0.004, maint_amount=0.0, max_leverage=5.0, min_liq_distance_mult=2.5,
    )
    assert 0 < lev <= 5.0
    # verify the resulting liq distance actually satisfies the rule
    from futures_fund.liquidation import liquidation_price
    margin = (qty := 20.0) * 100.0 / lev
    liq = liquidation_price(100.0, qty, margin, "long", 0.004, 0.0)
    assert liq_distance_ratio(100.0, 95.0, liq, "long") >= 2.5 - 1e-6


def test_choose_leverage_returns_cap_when_geometry_is_safe():
    # Tiny stop gap (0.1): even 50x keeps the liq price ~16x the stop gap away,
    # so the cap itself is safe and choose_leverage returns the cap unchanged.
    lev = choose_leverage(
        entry=100.0, stop=99.9, qty=10.0, direction="long",
        mmr=0.004, maint_amount=0.0, max_leverage=50.0, min_liq_distance_mult=2.5,
    )
    assert lev == pytest.approx(50.0)
