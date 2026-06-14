from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction, TradeProposal
from futures_fund.sentiment_decay import SentimentLevel

Rating = Literal["strong_long", "long", "flat", "short", "strong_short"]
Stance = Literal["bullish", "bearish", "neutral"]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    item_ids: list[str] = Field(default_factory=list)
    coins: list[str] = Field(default_factory=list)


class SentimentRead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Literal["flow", "narrative", "influencer"]
    coin: str
    stance: Stance
    level: SentimentLevel
    s: float = Field(ge=-1, le=1)
    confidence: float = Field(ge=0, le=1)
    claims: list[Claim] = Field(default_factory=list)
    rationale: str = ""
    as_of_ts: datetime


class ResearchPlan(BaseModel):
    symbol: str
    rating: Rating
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    falsifiable_prediction: str


# The decider emits a SentimentPlan; same shape as ResearchPlan.
SentimentPlan = ResearchPlan


class AgentProposal(BaseModel):
    symbol: str                       # raw exchange id, e.g. BTCUSDT (matches SymbolSpec.symbol)
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float]
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = 4.0
    rationale: str = ""
    falsifiable_prediction: str = ""  # from the RM plan -> journaled -> tested at HOLD/CLOSE
    confirmation: bool = True         # QuantAgent-style confirmation trigger
    risk_mult: float = 1.0            # optional per-trade risk REDUCTION; gate clamps to (0,1]


_RATING_DIRECTION: dict[str, Direction] = {
    "strong_long": "long", "long": "long", "short": "short", "strong_short": "short",
}


def rating_to_direction(rating: Rating) -> Direction | None:
    """5-tier research rating -> trade direction. 'flat' -> None (no trade)."""
    return _RATING_DIRECTION.get(rating)


def to_trade_proposal(ap: AgentProposal, funding_rate: float) -> TradeProposal:
    """Convert an agent's structured proposal into the A1 TradeProposal the risk gate consumes."""
    return TradeProposal(
        symbol=ap.symbol, direction=ap.direction, entry=ap.entry, stop=ap.stop,
        take_profits=ap.take_profits, atr=ap.atr, confidence=ap.confidence,
        horizon_hours=ap.horizon_hours, funding_rate=funding_rate, risk_mult=ap.risk_mult,
    )
