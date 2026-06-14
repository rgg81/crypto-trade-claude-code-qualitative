import pytest
from pydantic import ValidationError

from futures_fund.models import (
    MmrBracket,
    RiskDecision,
    SymbolSpec,
    TradeProposal,
)


def test_trade_proposal_rejects_bad_direction():
    with pytest.raises(ValidationError):
        TradeProposal(symbol="BTCUSDT", direction="up", entry=100.0, stop=95.0,
                      take_profits=[110.0], atr=2.0, confidence=0.6, horizon_hours=8,
                      funding_rate=0.0001)


def test_trade_proposal_long_stop_below_entry_ok():
    p = TradeProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                      take_profits=[110.0], atr=2.0, confidence=0.6, horizon_hours=8,
                      funding_rate=0.0001)
    assert p.risk_per_unit == pytest.approx(5.0)


def test_symbol_spec_bracket_lookup_orders_brackets():
    spec = SymbolSpec(
        symbol="BTCUSDT", tick_size=0.1, step_size=0.001, min_notional=5.0,
        mmr_brackets=[
            MmrBracket(
                notional_floor=50000, notional_cap=250000, mmr=0.01,
                maint_amount=50.0, max_leverage=25,
            ),
            MmrBracket(
                notional_floor=0, notional_cap=50000, mmr=0.004,
                maint_amount=0.0, max_leverage=125,
            ),
        ],
    )
    assert spec.sorted_brackets[0].notional_floor == 0


def test_risk_decision_resize_requires_sized_trade():
    with pytest.raises(ValidationError):
        RiskDecision(verdict="resize", reason="too big", sized_trade=None)
