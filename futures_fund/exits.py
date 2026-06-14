from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel

from futures_fund.costs import project_funding, trade_fee
from futures_fund.fills import close_side, fill_price
from futures_fund.models import Direction
from futures_fund.state import Position

ExitReason = Literal["liquidation", "stop", "take_profit", "close"]


class ClosedTrade(BaseModel):
    symbol: str
    direction: Direction
    decision_id: str | None
    entry: float
    exit_price: float
    qty: float
    reason: ExitReason
    gross_pnl: float
    exit_fee: float
    funding: float
    slippage: float
    realized_pnl: float


def _trigger(
    position: Position, bar_high: float, bar_low: float
) -> tuple[ExitReason, float] | None:
    """Pessimistic priority: liquidation > stop > take-profit."""
    tp = position.take_profits[0] if position.take_profits else None
    if position.direction == "long":
        if bar_low <= position.liq_price:
            return "liquidation", position.liq_price
        if bar_low <= position.stop:
            return "stop", position.stop
        if tp is not None and bar_high >= tp:
            return "take_profit", tp
    else:  # short
        if bar_high >= position.liq_price:
            return "liquidation", position.liq_price
        if bar_high >= position.stop:
            return "stop", position.stop
        if tp is not None and bar_low <= tp:
            return "take_profit", tp
    return None


def _gap_honest_exit_level(position: Position, reason: ExitReason, level: float,
                           bar_open: float | None) -> float:
    """The realistic fill LEVEL for an exit, never better than the trigger level (long/short
    symmetric). A STOP the bar OPENED beyond (gapped past) fills at the bar OPEN — the first live
    print — not the unreachable stop. A LIQUIDATION fills at/through the BANKRUPTCY price
    (entry -/+ margin/qty = the full isolated-margin loss), worse than the maintenance liq trigger.
    A TAKE-PROFIT is a reduce-only limit that fills at level-or-better, so booking the level is
    already conservative -> unchanged."""
    is_long = position.direction == "long"
    if reason == "liquidation" and position.qty > 0:
        bankruptcy = (position.entry - position.margin / position.qty) if is_long \
            else (position.entry + position.margin / position.qty)
        return min(level, bankruptcy) if is_long else max(level, bankruptcy)
    if reason == "stop" and bar_open is not None and math.isfinite(bar_open):
        return min(level, bar_open) if is_long else max(level, bar_open)
    return level


def detect_exit(
    position: Position, bar_high: float, bar_low: float, *,
    funding_rate: float, funding_events: int, slippage_bps: float, pay_bnb: bool = False,
    bar_open: float | None = None,
) -> ClosedTrade | None:
    """Return a ClosedTrade if the bar triggered an exit, else None. PnL is net of exit fee,
    accrued funding, and exit-side slippage. Exit fills are GAP-HONEST: a stop/liq the bar gapped
    past fills at the worse of (level, bar open) / the bankruptcy price — never at an unreachable
    level (see `_gap_honest_exit_level`). Reported `slippage` is the full adverse vs the trigger
    level (gap + bps)."""
    hit = _trigger(position, bar_high, bar_low)
    if hit is None:
        return None
    reason, level = hit
    fill_level = _gap_honest_exit_level(position, reason, level, bar_open)
    side = close_side(position.direction)
    exit_fill = fill_price(fill_level, side, slippage_bps)
    if position.direction == "long":
        gross = position.qty * (exit_fill - position.entry)
    else:
        gross = position.qty * (position.entry - exit_fill)
    exit_fee = trade_fee(position.qty * exit_fill, maker=False, pay_bnb=pay_bnb)
    # Signed funding: positive = we PAID it (reduces PnL), negative = we RECEIVED a credit
    # (raises PnL). Do NOT clamp to 0 — that silently drops carry credits on funding-receiving
    # trades (a long with negative funding, a short with positive funding).
    funding = project_funding(position.qty * position.entry, funding_rate,
                              position.direction, funding_events)
    slippage = abs(exit_fill - level) * position.qty
    realized = gross - exit_fee - funding
    return ClosedTrade(
        symbol=position.symbol, direction=position.direction, decision_id=position.decision_id,
        entry=position.entry, exit_price=exit_fill, qty=position.qty, reason=reason,
        gross_pnl=gross, exit_fee=exit_fee, funding=funding, slippage=slippage,
        realized_pnl=realized,
    )
