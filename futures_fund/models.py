from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

Direction = Literal["long", "short"]
RegimeQuadrant = Literal[
    "low_vol_trend", "high_vol_trend", "low_vol_range", "high_vol_range", "transition"
]
HealthTier = Literal["healthy", "caution", "stressed"]
Bias = Literal["normal", "reduce", "flat"]
Verdict = Literal["approve", "resize", "veto"]


class MmrBracket(BaseModel):
    notional_floor: float
    notional_cap: float
    mmr: float                      # maintenance margin rate
    maint_amount: float             # maintenance amount offset (cum)
    max_leverage: float


class SymbolSpec(BaseModel):
    symbol: str
    tick_size: float
    step_size: float
    min_notional: float
    mmr_brackets: list[MmrBracket]

    @property
    def sorted_brackets(self) -> list[MmrBracket]:
        return sorted(self.mmr_brackets, key=lambda b: b.notional_floor)


class TradeProposal(BaseModel):
    symbol: str
    direction: Direction
    entry: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    atr: float
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_hours: float = Field(gt=0)
    funding_rate: float             # current/predicted 8h funding rate (e.g. 0.0001)
    # 8h majors; 4h many perps; per-contract
    funding_interval_hours: float = Field(default=8.0, gt=0)
    # Optional per-trade risk REDUCTION (e.g. 0.5 = half-size an unproven-edge starter). Lenient
    # float so a stray value never drops a proposal; the risk gate CLAMPS it to (0,1] so it can only
    # ever SHRINK a position, never increase risk above the policy cap. Default 1.0 = no-op.
    risk_mult: float = 1.0

    @model_validator(mode="after")
    def _check_stop_side(self) -> TradeProposal:
        if self.direction == "long" and self.stop >= self.entry:
            raise ValueError("long stop must be below entry")
        if self.direction == "short" and self.stop <= self.entry:
            raise ValueError("short stop must be above entry")
        return self

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)


class CostEstimate(BaseModel):
    entry_fee: float = 0.0
    exit_fee: float = 0.0
    funding: float = 0.0
    slippage: float = 0.0

    @property
    def total(self) -> float:
        return self.entry_fee + self.exit_fee + self.funding + self.slippage


class SizedTrade(BaseModel):
    proposal: TradeProposal
    qty: float
    notional: float
    leverage: float
    margin: float
    liq_price: float
    cost: CostEstimate


class RegimeState(BaseModel):
    quadrant: RegimeQuadrant
    trend_direction: Literal["up", "down", "neutral"] = "neutral"
    hurst: float = 0.5


MoodState = Literal["euphoric", "greedy", "neutral", "fearful", "capitulation"]


class CrowdMood(BaseModel):
    mood: MoodState
    # how split the crowd is: 0 = consensus, 1 = maximally split
    dispersion: float = Field(ge=0, le=1)
    rationale: str = ""


# Pure crowd-mood -> regime mapping. Crowd EXTREMES (euphoric, capitulation) are the desk's
# contrarian opportunity but also the most dangerous, so they map to a high-vol quadrant with the
# tightest caps among the non-transition quadrants (high_vol_range). The plain trending moods
# (greedy, fearful) map to high_vol_trend, and a quiet neutral crowd maps to low_vol_range.
_MOOD_QUADRANT: dict[MoodState, RegimeQuadrant] = {
    "euphoric": "high_vol_range",
    "greedy": "high_vol_trend",
    "neutral": "low_vol_range",
    "fearful": "high_vol_trend",
    "capitulation": "high_vol_range",
}

# Bullish moods lean trend "up", bearish moods lean "down", an undecided crowd is "neutral".
_MOOD_TREND: dict[MoodState, Literal["up", "down", "neutral"]] = {
    "euphoric": "up",
    "greedy": "up",
    "neutral": "neutral",
    "fearful": "down",
    "capitulation": "down",
}

# A crowd that is highly split (dispersion >= this) is bumped toward the most cautious quadrant.
_HIGH_DISPERSION = 0.6


def mood_to_regime(mood: MoodState, dispersion: float) -> RegimeState:
    """Deterministically map a crowd mood + dispersion to an existing RegimeQuadrant.

    The unchanged risk-caps machinery (``policy.caps_for``) reads the returned RegimeState and
    derives a sane posture: extremes and trending moods land in tighter high-vol quadrants, while a
    highly split crowd (``dispersion >= 0.6``) is bumped to ``transition`` (the most cautious
    quadrant, ``reduce`` bias). The trend direction is preserved regardless of the dispersion bump.
    """
    quadrant: RegimeQuadrant = (
        "transition" if dispersion >= _HIGH_DISPERSION else _MOOD_QUADRANT[mood]
    )
    return RegimeState(quadrant=quadrant, trend_direction=_MOOD_TREND[mood])


class PortfolioHealth(BaseModel):
    equity: float
    peak_equity: float
    open_heat: float = 0.0          # fraction of equity currently at risk (0..1)
    recent_hit_rate: float = 0.5

    @property
    def drawdown_from_peak(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def tier(self) -> HealthTier:
        dd = self.drawdown_from_peak
        if dd > 0.10:
            return "stressed"
        if dd >= 0.05:
            return "caution"
        return "healthy"


class RiskCaps(BaseModel):
    max_leverage: float
    per_trade_risk_pct: float       # fraction of equity, e.g. 0.01
    max_heat: float                 # fraction of equity, e.g. 0.10
    bias: Bias


class RiskDecision(BaseModel):
    verdict: Verdict
    reason: str
    sized_trade: SizedTrade | None = None
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_sized(self) -> RiskDecision:
        if self.verdict in ("approve", "resize") and self.sized_trade is None:
            raise ValueError(f"verdict '{self.verdict}' requires a sized_trade")
        return self
