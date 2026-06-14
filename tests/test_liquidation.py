import pytest

from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.models import MmrBracket

BRACKETS = [
    MmrBracket(
        notional_floor=0, notional_cap=50_000, mmr=0.004, maint_amount=0.0, max_leverage=125
    ),
    MmrBracket(
        notional_floor=50_000,
        notional_cap=250_000,
        mmr=0.005,
        maint_amount=50.0,
        max_leverage=100,
    ),
]


def test_mmr_lookup_low_bracket():
    mmr, maint = mmr_for_notional(10_000.0, BRACKETS)
    assert (mmr, maint) == (0.004, 0.0)


def test_mmr_lookup_high_bracket():
    mmr, maint = mmr_for_notional(100_000.0, BRACKETS)
    assert (mmr, maint) == (0.005, 50.0)


def test_mmr_above_top_bracket_uses_top():
    mmr, maint = mmr_for_notional(10_000_000.0, BRACKETS)
    assert (mmr, maint) == (0.005, 50.0)


def test_long_liquidation_below_entry():
    liq = liquidation_price(entry=100.0, qty=100.0, margin=1000.0, direction="long",
                            mmr=0.004, maint_amount=0.0)
    assert liq == pytest.approx(9000 / 99.6, rel=1e-9)
    assert liq < 100.0


def test_short_liquidation_above_entry():
    liq = liquidation_price(entry=100.0, qty=100.0, margin=1000.0, direction="short",
                            mmr=0.004, maint_amount=0.0)
    assert liq == pytest.approx(11000 / 100.4, rel=1e-9)
    assert liq > 100.0


def test_higher_leverage_moves_liq_closer_to_entry():
    far = liquidation_price(
        100.0, 100.0, margin=2000.0, direction="long", mmr=0.004, maint_amount=0.0
    )
    near = liquidation_price(
        100.0, 100.0, margin=500.0, direction="long", mmr=0.004, maint_amount=0.0
    )
    assert abs(100.0 - near) < abs(100.0 - far)
