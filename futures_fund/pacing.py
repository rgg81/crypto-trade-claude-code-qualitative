"""Monthly risk-pacing engine (Pillar 1 — DEPLOY: actively pursue 5%/month).

The desk sat ~flat for ~14 cycles ignoring the return target because the risk policy is
ONE-DIRECTIONAL — `policy.circuit_breaker` only ever DE-risks; nothing scales deployment UP when
BEHIND the target. This module adds the missing upward pressure, SAFELY:

- Calendar-month pacing: month-to-date return vs the pro-rated 5% pace.
- START SOFT early in the month; PRESS (deploy more) when BEHIND pace AND under-deployed AND NOT in
  drawdown; THROTTLE once the target is hit.
- ANTI-MARTINGALE (hard invariant): being behind because of DRAWDOWN never presses — the protected
  drawdown breakers own the loss path; pacing only ever spends UNUSED budget, never doubles into
  losses. `drawdown >= CAUTION_DD` forces mode <= soft.

It is ADVISORY/UTILIZATION-only: it raises how fully the desk uses its EXISTING gate-enforced caps
(more setups, fuller `risk_mult` toward 1.0, a lower take-it bar) — it NEVER raises a cap and NEVER
touches a protected module (risk_gate/policy/sizing). The risk gate still clamps `risk_mult` to
(0,1], so pacing can never increase risk above the survival cap.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import datetime

CAUTION_DD = 0.05      # drawdown at/above this -> NEVER press (mirrors policy caution / step-down)
SOFT_DAYS = 5.0        # first ~5 days of the month: start soft, preserve budget/optionality
PRESS_GAP = 0.01       # behind pro-rated pace by more than this (1% equity) -> press-eligible
PRESS_UTIL_HEAT = 0.04  # open heat below this (4% equity at risk) -> under-deployed, room to deploy

_MODE_RISK_MULT = {"throttle": 0.5, "soft": 0.5, "normal": 0.75, "press": 1.0}
_MODE_APPETITE = {"throttle": 0.25, "soft": 0.4, "normal": 0.6, "press": 0.9}


@dataclass
class PacingState:
    mode: str               # 'soft' | 'normal' | 'press' | 'throttle'
    appetite: float         # 0..1 deployment appetite
    suggested_risk_mult: float
    mtd_return: float       # month-to-date return vs the month-start anchor
    pace: float             # pro-rated target for days elapsed
    pace_gap: float         # mtd_return - pace (negative = behind)
    drawdown: float
    open_heat: float
    in_drawdown: bool
    days_elapsed: float
    days_in_month: int
    directive: str          # human guidance injected into the team's prompts


def _directive(mode: str, pace_gap: float, mtd: float, target: float) -> str:
    if mode == "throttle":
        return (f"THROTTLE — month-to-date {mtd:+.1%} has reached the {target:.0%} target. Be "
                f"selective, bank winners, protect the month; only take A+ setups.")
    if mode == "press":
        return (f"PRESS — behind pace by {pace_gap:+.1%} with unused budget, no drawdown. "
                f"DEPLOY: take EVERY gate-clearing edge-aligned setup at full size (risk_mult "
                f"1.0), lower the take-it bar, hunt setups across ALL regimes (trend, "
                f"range/mean-reversion, relative-value). Rate `flat` ONLY on a failed thesis, "
                f"never for want of looking — flat has negative carry vs the {target:.0%}/mo goal.")
    if mode == "soft":
        return ("SOFT — start the month conservatively (early month, or in drawdown deferring to "
                "the breakers). Take only clean high-conviction setups; preserve optionality.")
    return ("NORMAL — roughly on pace. Take clean edge-aligned setups at standard size; keep the "
            "book working toward the monthly target.")


def compute_pacing(*, mtd_return: float, days_elapsed: float, days_in_month: int,
                   drawdown: float, open_heat: float, monthly_target: float = 0.05) -> PacingState:
    """Pure pacing logic (no I/O). See module docstring for the safety invariants."""
    dim = max(1, int(days_in_month))
    pace = monthly_target * (max(0.0, days_elapsed) / dim)
    pace_gap = mtd_return - pace
    in_dd = drawdown >= CAUTION_DD

    if mtd_return >= monthly_target:
        mode = "throttle"
    elif in_dd:
        mode = "soft"                       # ANTI-MARTINGALE: drawdown never presses
    elif days_elapsed < SOFT_DAYS:
        mode = "soft"                       # start the month soft
    elif pace_gap <= -PRESS_GAP and open_heat < PRESS_UTIL_HEAT:
        mode = "press"                      # behind + under-deployed + healthy -> deploy
    else:
        mode = "normal"

    return PacingState(
        mode=mode, appetite=_MODE_APPETITE[mode], suggested_risk_mult=_MODE_RISK_MULT[mode],
        mtd_return=mtd_return, pace=pace, pace_gap=pace_gap, drawdown=drawdown, open_heat=open_heat,
        in_drawdown=in_dd, days_elapsed=days_elapsed, days_in_month=dim,
        directive=_directive(mode, pace_gap, mtd_return, monthly_target),
    )


def _month_start(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def pacing_state(state_dir, now: datetime, health, *, monthly_target: float = 0.05) -> PacingState:
    """Compute the calendar-month pacing state from the equity log + live portfolio health.

    `health` supplies `drawdown_from_peak` and `open_heat`. Month-to-date return is measured vs the
    last equity point at/before the 1st-of-month anchor (or the earliest point if the desk started
    mid-month). FAIL-SAFE: with <2 in-month points (no basis) -> SOFT (conservative default), so a
    cold start or a thin log never presses."""
    from futures_fund.equity_log import equity_series
    dd = float(getattr(health, "drawdown_from_peak", 0.0) or 0.0)
    heat = float(getattr(health, "open_heat", 0.0) or 0.0)
    dim = calendar.monthrange(now.year, now.month)[1]
    days_elapsed = (now - _month_start(now)).total_seconds() / 86400.0

    series = []
    for ts, eq in equity_series(state_dir):
        try:
            series.append((datetime.fromisoformat(ts), float(eq)))
        except (ValueError, TypeError):
            continue
    if len(series) < 2:
        return compute_pacing(mtd_return=0.0, days_elapsed=min(days_elapsed, SOFT_DAYS - 0.01),
                              days_in_month=dim, drawdown=dd, open_heat=heat,
                              monthly_target=monthly_target)
    anchor_ts = _month_start(now)
    at_or_before = [eq for ts, eq in series if ts <= anchor_ts]
    base = at_or_before[-1] if at_or_before else series[0][1]
    last = series[-1][1]
    mtd = (last / base - 1.0) if base > 0 else 0.0
    return compute_pacing(mtd_return=mtd, days_elapsed=days_elapsed, days_in_month=dim,
                          drawdown=dd, open_heat=heat, monthly_target=monthly_target)
