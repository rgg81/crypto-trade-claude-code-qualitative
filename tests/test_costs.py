from datetime import UTC, datetime

import pytest

from futures_fund.costs import (
    count_funding_events,
    project_funding,
    round_trip_fee,
    slippage_cost,
    trade_fee,
    vwap_fill,
)


def _utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# --- fees ---
def test_taker_fee_is_5bps_of_notional():
    assert trade_fee(notional=10_000.0, maker=False) == pytest.approx(5.0)  # 0.05%


def test_maker_fee_is_2bps_of_notional():
    assert trade_fee(notional=10_000.0, maker=True) == pytest.approx(2.0)   # 0.02%


def test_bnb_discount_applies_10pct():
    assert trade_fee(10_000.0, maker=False, pay_bnb=True) == pytest.approx(4.5)


def test_round_trip_taker_in_and_out():
    assert round_trip_fee(10_000.0, maker_entry=False, maker_exit=False) == pytest.approx(10.0)


# --- funding ---
def test_count_funding_events_crossing_two_boundaries():
    # 07:00 -> 17:00 UTC crosses the 08:00 and 16:00 settlements = 2
    n = count_funding_events(_utc(2026, 5, 29, 7, 0), _utc(2026, 5, 29, 17, 0))
    assert n == 2


def test_count_funding_events_none_within_window():
    # 09:00 -> 15:00 crosses no boundary
    assert count_funding_events(_utc(2026, 5, 29, 9), _utc(2026, 5, 29, 15)) == 0


def test_count_funding_events_4h_interval_more_events():
    # 4h funding: 07:00 -> 17:00 crosses 08:00, 12:00, 16:00 = 3
    n = count_funding_events(_utc(2026, 5, 29, 7, 0), _utc(2026, 5, 29, 17, 0), interval_hours=4)
    assert n == 3


def test_long_pays_positive_funding():
    cost = project_funding(notional=10_000.0, funding_rate=0.0001, direction="long", n_events=2)
    assert cost == pytest.approx(2.0)


def test_short_receives_positive_funding():
    cost = project_funding(notional=10_000.0, funding_rate=0.0001, direction="short", n_events=2)
    assert cost == pytest.approx(-2.0)


# --- slippage ---
def test_vwap_fill_single_level():
    filled, vwap = vwap_fill([(100.0, 10.0)], qty=5.0)
    assert filled == pytest.approx(5.0)
    assert vwap == pytest.approx(100.0)


def test_vwap_fill_walks_multiple_levels():
    filled, vwap = vwap_fill([(100.0, 10.0), (101.0, 5.0)], qty=15.0)
    assert filled == pytest.approx(15.0)
    assert vwap == pytest.approx((1000 + 505) / 15)


def test_vwap_fill_insufficient_depth_returns_partial():
    filled, vwap = vwap_fill([(100.0, 10.0)], qty=25.0)
    assert filled == pytest.approx(10.0)
    assert vwap == pytest.approx(100.0)


def test_slippage_cost_is_qty_times_price_diff_from_reference():
    cost = slippage_cost([(100.0, 10.0), (101.0, 10.0)], qty=15.0, reference_price=100.0)
    # fill 10@100 + 5@101 = vwap 100.333..., diff 0.333.. * 15 = 5.0
    assert cost == pytest.approx(5.0)
