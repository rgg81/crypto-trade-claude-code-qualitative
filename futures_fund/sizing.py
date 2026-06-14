from __future__ import annotations

from futures_fund.liquidation import liquidation_price
from futures_fund.models import Direction


def qty_from_risk(equity: float, risk_pct: float, entry: float, stop: float) -> float:
    """Fixed-fractional sizing: qty such that a stop-out loses exactly equity*risk_pct."""
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return 0.0
    return (equity * risk_pct) / risk_per_unit


def liq_distance_ratio(entry: float, stop: float, liq_price: float, direction: Direction) -> float:
    """How many 'stop distances' away the liquidation price sits from entry."""
    stop_gap = abs(entry - stop)
    if stop_gap <= 0:
        return float("inf")
    return abs(entry - liq_price) / stop_gap


def choose_leverage(
    entry: float, stop: float, qty: float, direction: Direction,
    mmr: float, maint_amount: float, max_leverage: float,
    min_liq_distance_mult: float = 2.5,
) -> float:
    """Pick the highest leverage <= max_leverage that keeps liq distance >= mult*stop_gap.

    Leverage is an OUTPUT of the risk geometry, never an input. Searches leverage
    downward from max_leverage; lower leverage => more margin => liq farther from entry.
    """
    if qty <= 0:
        return 0.0
    notional = qty * entry
    # Scan candidate leverages from cap down to 1x in fine steps; pick first that is safe.
    steps = 200
    best = 0.0
    for i in range(steps + 1):
        lev = max_leverage - (max_leverage - 1.0) * (i / steps)
        lev = max(1.0, lev)
        margin = notional / lev
        liq = liquidation_price(entry, qty, margin, direction, mmr, maint_amount)
        if liq_distance_ratio(entry, stop, liq, direction) >= min_liq_distance_mult:
            best = lev
            break
    return best
