from datetime import UTC, datetime

import pytest

from futures_fund.executor import close_at_mark, open_position, reconcile
from futures_fund.models import CostEstimate, SizedTrade, TradeProposal
from tests.test_state import _pos


def _sized(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0):
    prop = TradeProposal(symbol=symbol, direction=direction, entry=entry, stop=stop,
                         take_profits=[entry * 1.15], atr=2.0, confidence=0.6,
                         horizon_hours=4, funding_rate=0.0)
    return SizedTrade(proposal=prop, qty=0.5, notional=entry * 0.5, leverage=5.0,
                      margin=entry * 0.5 / 5.0, liq_price=82.0, cost=CostEstimate())


def test_reconcile_opens_new_and_closes_removed():
    current = [_pos("BTCUSDT", "long")]
    target = {"ETHUSDT": _sized("ETHUSDT", "long")}   # BTC not in target, ETH new
    to_open, to_close = reconcile(target, current)
    assert [st.proposal.symbol for st in to_open] == ["ETHUSDT"]
    assert [p.symbol for p in to_close] == ["BTCUSDT"]


def test_reconcile_keeps_unchanged_symbol():
    current = [_pos("BTCUSDT", "long")]
    target = {"BTCUSDT": _sized("BTCUSDT", "long")}   # same symbol+direction held
    to_open, to_close = reconcile(target, current)
    assert to_open == [] and to_close == []


def test_reconcile_flips_direction_closes_then_opens():
    current = [_pos("BTCUSDT", "long")]
    target = {"BTCUSDT": _sized("BTCUSDT", "short", entry=100.0, stop=105.0)}
    to_open, to_close = reconcile(target, current)
    assert [st.proposal.direction for st in to_open] == ["short"]
    assert [p.symbol for p in to_close] == ["BTCUSDT"]


def test_open_position_applies_entry_slippage_and_returns_fee():
    st = _sized("BTCUSDT", "long", entry=100.0)
    pos, entry_fee = open_position(st, cycle=1, ts=datetime(2026, 5, 29, tzinfo=UTC),
                                   slippage_bps=10, decision_id="d1")
    assert pos.entry == pytest.approx(100.1)        # buy slipped up
    assert pos.symbol == "BTCUSDT" and pos.decision_id == "d1"
    assert entry_fee == pytest.approx(0.5 * 100.1 * 0.0005)  # taker fee on notional


def test_close_at_mark_realizes_pnl_net_of_fee():
    pos = _pos("BTCUSDT", "long")  # qty 0.5 entry 100
    ct = close_at_mark(pos, mark=110.0, funding_rate=0.0, funding_events=0, slippage_bps=0)
    assert ct.reason == "close"
    assert ct.realized_pnl == pytest.approx(0.5 * (110.0 - 100.0) - ct.exit_fee)


def test_close_at_mark_credits_received_funding():
    # holdings-close path: a long with NEGATIVE funding RECEIVES a credit -> raises PnL
    # (the max(0,...) clamp used to drop it). 0.5*100 * -0.001 * 2 = -0.1 credit.
    pos = _pos("BTCUSDT", "long")
    ct = close_at_mark(pos, mark=110.0, funding_rate=-0.001, funding_events=2, slippage_bps=0)
    assert ct.funding == pytest.approx(-0.1) and ct.funding < 0
    assert ct.realized_pnl == pytest.approx(0.5 * (110.0 - 100.0) - ct.exit_fee - ct.funding)
