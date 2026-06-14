import pytest

from futures_fund.exits import ClosedTrade, detect_exit
from tests.test_state import _pos  # Position factory (long stop 95, short stop 105)


def _long(**over):
    p = _pos("BTCUSDT", "long")  # qty 0.5, entry 100, stop 95, tp 115, liq 82
    return p.model_copy(update=over)


def _short(**over):
    p = _pos("ETHUSDT", "short")  # qty 0.5, entry 100, stop 105, tp 115(!), liq 82(!)
    # fix short tp/liq to valid short geometry: tp below entry, liq above entry
    return p.model_copy(update={"take_profits": [85.0], "liq_price": 118.0, **over})


def test_short_receiving_funding_credit_raises_pnl():
    # A SHORT with a POSITIVE funding rate RECEIVES funding (a credit) -> raises realized PnL.
    # The old max(0, ...) clamp silently dropped the credit (understating PnL on carry trades).
    ct = detect_exit(_short(), bar_high=99.0, bar_low=84.0,  # TP 85 fires
                     funding_rate=0.001, funding_events=2, slippage_bps=0)
    assert ct.funding == pytest.approx(-0.1)  # 0.5*100 * 0.001 * 2 = 0.1 credit (negative=received)
    assert ct.funding < 0
    assert ct.realized_pnl == pytest.approx(ct.gross_pnl - ct.exit_fee - ct.funding)


def test_long_receiving_funding_credit_raises_pnl():
    # A LONG with a NEGATIVE funding rate RECEIVES funding (mirrors our real INJ/HYPE longs).
    ct = detect_exit(_long(), bar_high=116.0, bar_low=99.0,  # TP 115 fires
                     funding_rate=-0.001, funding_events=2, slippage_bps=0)
    assert ct.funding == pytest.approx(-0.1) and ct.funding < 0
    assert ct.realized_pnl == pytest.approx(ct.gross_pnl - ct.exit_fee - ct.funding)


def test_no_trigger_returns_none():
    # bar stays between stop and tp
    assert detect_exit(_long(), bar_high=108.0, bar_low=99.0,
                       funding_rate=0.0, funding_events=0, slippage_bps=0) is None


def test_long_stop_hit_realizes_loss():
    ct = detect_exit(_long(), bar_high=101.0, bar_low=94.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert isinstance(ct, ClosedTrade)
    assert ct.reason == "stop"
    assert ct.exit_price == pytest.approx(95.0)            # no slippage
    # gross -2.5 minus the exit fee (~0.024); abs tolerance covers the fee
    assert ct.realized_pnl == pytest.approx(0.5 * (95.0 - 100.0), abs=0.05)
    assert ct.realized_pnl < 0


def test_long_take_profit_hit_realizes_gain():
    ct = detect_exit(_long(), bar_high=116.0, bar_low=99.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "take_profit"
    assert ct.exit_price == pytest.approx(115.0)
    assert ct.realized_pnl > 0


def test_long_liquidation_fills_at_bankruptcy_price_not_maintenance_liq():
    # FIX 3: an isolated liq closes at/through the BANKRUPTCY price (entry - margin/qty = 100 - 20 =
    # 80 for qty 0.5 / margin 10), the FULL-margin loss — not the maintenance liq 82 (partial).
    # Still takes priority over the stop (bar low 80 below both).
    ct = detect_exit(_long(), bar_high=101.0, bar_low=80.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "liquidation"
    assert ct.exit_price == pytest.approx(80.0)            # bankruptcy, not 82
    assert ct.gross_pnl == pytest.approx(0.5 * (80.0 - 100.0))  # -10 = full margin
    assert abs(ct.realized_pnl) >= 10.0                    # >= the isolated margin


def test_short_liquidation_fills_at_bankruptcy_price():
    # mirror: short bankruptcy = entry + margin/qty = 100 + 20 = 120 (vs maintenance liq 118).
    ct = detect_exit(_short(), bar_high=121.0, bar_low=99.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "liquidation"
    assert ct.exit_price == pytest.approx(120.0)
    assert abs(ct.realized_pnl) >= 10.0


def test_long_stop_gap_down_fills_at_bar_open_not_stop():
    # FIX 2: the bar GAPPED DOWN through the stop (open 90, high 92 < stop 95) -> a live stop-market
    # fills at the open 90 (worse), not the unreachable 95. Loss is honestly larger.
    ct = detect_exit(_long(), bar_high=92.0, bar_low=88.0, bar_open=90.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"
    assert ct.exit_price == pytest.approx(90.0)            # gap-open, not the 95 stop
    assert ct.gross_pnl == pytest.approx(0.5 * (90.0 - 100.0))  # -5.0, worse than -2.5 at the stop


def test_short_stop_gap_up_fills_at_bar_open_not_stop():
    # mirror: gapped UP through the short's stop 105 (open 110, low 108 > 105) -> fill at open 110.
    ct = detect_exit(_short(), bar_high=112.0, bar_low=108.0, bar_open=110.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"
    assert ct.exit_price == pytest.approx(110.0)           # gap-open, not the 105 stop
    assert ct.gross_pnl == pytest.approx(0.5 * (100.0 - 110.0))  # -5.0, worse than -2.5


def test_within_range_stop_still_fills_at_stop_level():
    # no gap (bar opened ABOVE the stop and fell through it) -> fills at the stop level, unchanged.
    ct = detect_exit(_long(), bar_high=101.0, bar_low=94.0, bar_open=99.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop" and ct.exit_price == pytest.approx(95.0)


def test_tp_gap_through_still_books_at_tp_level():
    # a TP (reduce-only limit) fills at level-or-better; booking the level on a gap-through is
    # conservative -> a long TP 115 with the bar gapping UP through it still books 115 (not 116).
    ct = detect_exit(_long(), bar_high=120.0, bar_low=116.0, bar_open=116.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "take_profit" and ct.exit_price == pytest.approx(115.0)


def test_long_stop_beats_tp_when_both_touched():
    # both stop (low<=95) and tp (high>=115) in the same bar -> pessimistic: stop
    ct = detect_exit(_long(), bar_high=120.0, bar_low=94.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"


def test_short_stop_hit_above_entry():
    ct = detect_exit(_short(), bar_high=106.0, bar_low=99.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "stop"
    assert ct.exit_price == pytest.approx(105.0)
    # gross -2.5 minus the exit fee (~0.026); abs tolerance covers the fee
    assert ct.realized_pnl == pytest.approx(0.5 * (100.0 - 105.0), abs=0.05)


def test_short_take_profit_below_entry():
    ct = detect_exit(_short(), bar_high=101.0, bar_low=84.0,
                     funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "take_profit"
    assert ct.exit_price == pytest.approx(85.0)
    assert ct.realized_pnl > 0


def test_funding_reduces_realized_pnl_for_long():
    # positive funding, long pays; 1 event on notional ~50 -> small cost
    ct_no = detect_exit(_long(), bar_high=116.0, bar_low=99.0,
                        funding_rate=0.001, funding_events=0, slippage_bps=0)
    ct_fund = detect_exit(_long(), bar_high=116.0, bar_low=99.0,
                          funding_rate=0.001, funding_events=2, slippage_bps=0)
    assert ct_fund.realized_pnl < ct_no.realized_pnl
    assert ct_fund.funding > 0
