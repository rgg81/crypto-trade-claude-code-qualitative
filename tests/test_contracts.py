from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from futures_fund.contracts import (
    AgentProposal,
    Claim,
    ResearchPlan,
    SentimentPlan,
    SentimentRead,
    rating_to_direction,
    to_trade_proposal,
)
from futures_fund.models import TradeProposal


def test_rating_to_direction_maps_five_tiers():
    assert rating_to_direction("strong_long") == "long"
    assert rating_to_direction("long") == "long"
    assert rating_to_direction("short") == "short"
    assert rating_to_direction("strong_short") == "short"
    assert rating_to_direction("flat") is None


def test_research_plan_requires_falsifiable_prediction():
    with pytest.raises(ValidationError):
        ResearchPlan(symbol="BTCUSDT", rating="long", confidence=0.7, thesis="up only")


def test_sentiment_plan_is_research_plan():
    assert SentimentPlan is ResearchPlan
    sp = SentimentPlan(symbol="BTCUSDT", rating="strong_long", confidence=0.8,
                       thesis="flows turning", falsifiable_prediction="funding flips +")
    assert isinstance(sp, ResearchPlan)


def test_to_trade_proposal_maps_fields_and_injects_funding():
    ap = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                       take_profits=[115.0], atr=2.0, confidence=0.7, horizon_hours=8,
                       rationale="trend + funding tailwind")
    tp = to_trade_proposal(ap, funding_rate=0.0001)
    assert isinstance(tp, TradeProposal)
    assert tp.symbol == "BTCUSDT" and tp.direction == "long"
    assert tp.funding_rate == 0.0001
    assert tp.risk_per_unit == pytest.approx(5.0)


def test_to_trade_proposal_threads_risk_mult():
    # the optional per-trade risk_mult must flow AgentProposal -> TradeProposal (default 1.0)
    ap = AgentProposal(symbol="BTCUSDT", direction="short", entry=100.0, stop=105.0,
                       take_profits=[90.0], atr=2.0, confidence=0.6, risk_mult=0.5)
    tp = to_trade_proposal(ap, funding_rate=0.0001)
    assert tp.risk_mult == 0.5
    # default path unchanged
    ap2 = AgentProposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                        take_profits=[110.0], atr=2.0, confidence=0.6)
    assert to_trade_proposal(ap2, 0.0001).risk_mult == 1.0


def test_claim_valid_construction():
    c = Claim(text="ETF inflows spiking", item_ids=["a1", "b2"], coins=["BTC", "ETH"])
    assert c.text == "ETF inflows spiking"
    assert c.item_ids == ["a1", "b2"]
    assert c.coins == ["BTC", "ETH"]


def test_claim_defaults_empty_lists():
    c = Claim(text="rumor only")
    assert c.item_ids == []
    assert c.coins == []


def test_claim_forbids_extra_fields():
    with pytest.raises(ValidationError):
        Claim(text="x", source="twitter")


def test_sentiment_read_valid_construction():
    ts = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)
    sr = SentimentRead(
        agent="flow", coin="BTC", stance="bullish", level="positive",
        s=0.5, confidence=0.7,
        claims=[Claim(text="net inflows", item_ids=["i1"], coins=["BTC"])],
        rationale="spot bids", as_of_ts=ts,
    )
    assert sr.agent == "flow"
    assert sr.coin == "BTC"
    assert sr.level == "positive"
    assert sr.s == 0.5
    assert sr.confidence == 0.7
    assert sr.claims[0].text == "net inflows"
    assert sr.as_of_ts == ts


def test_sentiment_read_defaults_claims_and_rationale():
    sr = SentimentRead(agent="narrative", coin="ETH", stance="neutral", level="neutral",
                       s=0.0, confidence=0.5, as_of_ts=datetime(2026, 6, 13, tzinfo=UTC))
    assert sr.claims == []
    assert sr.rationale == ""


def test_sentiment_read_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SentimentRead(agent="flow", coin="BTC", stance="bullish", level="positive",
                      s=0.5, confidence=0.7, as_of_ts=datetime(2026, 6, 13, tzinfo=UTC),
                      extra="nope")


def test_sentiment_read_rejects_bad_agent():
    with pytest.raises(ValidationError):
        SentimentRead(agent="oracle", coin="BTC", stance="bullish", level="positive",
                      s=0.5, confidence=0.7, as_of_ts=datetime(2026, 6, 13, tzinfo=UTC))


@pytest.mark.parametrize("bad_s", [-1.5, 1.01])
def test_sentiment_read_s_bounds(bad_s):
    with pytest.raises(ValidationError):
        SentimentRead(agent="flow", coin="BTC", stance="bullish", level="positive",
                      s=bad_s, confidence=0.7, as_of_ts=datetime(2026, 6, 13, tzinfo=UTC))


@pytest.mark.parametrize("good_s", [-1.0, 0.0, 1.0])
def test_sentiment_read_s_bounds_inclusive(good_s):
    sr = SentimentRead(agent="flow", coin="BTC", stance="neutral", level="neutral",
                       s=good_s, confidence=0.5, as_of_ts=datetime(2026, 6, 13, tzinfo=UTC))
    assert sr.s == good_s


@pytest.mark.parametrize("bad_c", [-0.01, 1.5])
def test_sentiment_read_confidence_bounds(bad_c):
    with pytest.raises(ValidationError):
        SentimentRead(agent="flow", coin="BTC", stance="bullish", level="positive",
                      s=0.5, confidence=bad_c, as_of_ts=datetime(2026, 6, 13, tzinfo=UTC))


def test_sentiment_read_requires_as_of_ts():
    with pytest.raises(ValidationError):
        SentimentRead(agent="flow", coin="BTC", stance="bullish", level="positive",
                      s=0.5, confidence=0.7)
