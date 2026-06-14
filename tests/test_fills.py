import pytest

from futures_fund.fills import close_side, fill_price, open_side


def test_buy_fill_slips_up_sell_slips_down():
    assert fill_price(100.0, "buy", slippage_bps=10) == pytest.approx(100.1)   # +0.10%
    assert fill_price(100.0, "sell", slippage_bps=10) == pytest.approx(99.9)


def test_zero_slippage_is_identity():
    assert fill_price(100.0, "buy", slippage_bps=0) == 100.0


def test_open_side_maps_direction_to_order_side():
    assert open_side("long") == "buy"
    assert open_side("short") == "sell"


def test_close_side_is_the_opposite():
    assert close_side("long") == "sell"
    assert close_side("short") == "buy"
