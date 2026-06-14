from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from futures_fund.models import PortfolioHealth, RegimeQuadrant, RegimeState, RiskCaps

# Healthy-tier base caps per regime quadrant: (max_leverage, per_trade_risk_pct, max_heat)
# MEDIUM-AGGRESSIVE profile: 1%/month is a FLOOR to beat, hunt above it.
_BASE_CAPS: dict[RegimeQuadrant, tuple[float, float, float]] = {
    "low_vol_trend":  (8.0, 0.020, 0.18),
    "high_vol_trend": (6.0, 0.015, 0.14),
    "low_vol_range":  (5.0, 0.015, 0.12),
    "high_vol_range": (3.0, 0.010, 0.07),
    "transition":     (3.0, 0.010, 0.06),
}


def caps_for(regime: RegimeState, health: PortfolioHealth) -> RiskCaps:
    """Adaptive caps from the regime × portfolio-health matrix (spec §7.1)."""
    lev, risk, heat = _BASE_CAPS[regime.quadrant]
    bias = "reduce" if regime.quadrant == "transition" else "normal"
    tier = health.tier

    if tier == "stressed":
        return RiskCaps(max_leverage=1.0, per_trade_risk_pct=0.0, max_heat=0.0, bias="flat")
    if tier == "caution":
        lev *= 0.5
        risk *= 0.5
        heat *= 0.5
        bias = "reduce"
    return RiskCaps(max_leverage=lev, per_trade_risk_pct=risk, max_heat=heat, bias=bias)


class BreakerState(BaseModel):
    allow_new_entries: bool
    force_flatten: bool
    risk_multiplier: float
    reason: str = ""


def circuit_breaker(
    daily_pnl_pct: float, weekly_pnl_pct: float, monthly_pnl_pct: float, dd_from_peak: float
) -> BreakerState:
    """Hard circuit breakers (spec §7). Thresholds are fractions (e.g. -0.03 = -3%)."""
    allow_new = True
    force_flatten = False
    mult = 1.0
    reasons: list[str] = []

    if dd_from_peak >= 0.05:           # step-down: halve risk past -5% from peak
        mult = 0.5
        reasons.append("dd>=5% step-down")
    if dd_from_peak >= 0.12:           # deeper step-down: cut to 0.35x past -12% from peak
        mult = 0.35
        reasons.append("dd>=12% step-down")
    if daily_pnl_pct <= -0.06:
        allow_new = False
        reasons.append("daily<=-6% halt-new")
    if weekly_pnl_pct <= -0.12:
        allow_new = False
        reasons.append("weekly<=-12% halt-new")
    if monthly_pnl_pct <= -0.20:
        allow_new = False
        reasons.append("monthly<=-20% halt-new")
    if dd_from_peak >= 0.22:           # hard kill-switch: force-flatten + halt all new entries
        allow_new = False
        force_flatten = True
        reasons.append("dd>=22% force-flatten")
    return BreakerState(allow_new_entries=allow_new, force_flatten=force_flatten,
                        risk_multiplier=mult, reason="; ".join(reasons))


def cvar(returns: list[float], alpha: float = 0.05) -> float:
    """Conditional VaR (expected shortfall): mean of the worst `alpha` fraction of returns.

    Returns 0.0 if there are no observations. More negative = worse tail.
    """
    if not returns:
        return 0.0
    arr = np.sort(np.asarray(returns, dtype=float))
    k = max(1, int(np.ceil(alpha * len(arr))))
    return float(arr[:k].mean())
