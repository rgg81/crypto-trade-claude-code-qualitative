from __future__ import annotations

from datetime import datetime

from futures_fund.costs import project_funding, trade_fee
from futures_fund.exits import ClosedTrade
from futures_fund.fills import close_side, fill_price, open_side
from futures_fund.models import SizedTrade
from futures_fund.state import Position


def reconcile(
    target: dict[str, SizedTrade], current: list[Position]
) -> tuple[list[SizedTrade], list[Position]]:
    """Diff desired book vs open positions. Returns (to_open, to_close).
    A symbol held in the same direction is left untouched; a direction flip closes then reopens."""
    held = {p.symbol: p for p in current}
    to_open: list[SizedTrade] = []
    to_close: list[Position] = []
    for sym, st in target.items():
        cur = held.get(sym)
        if cur is None or cur.direction != st.proposal.direction:
            to_open.append(st)
    for p in current:
        st = target.get(p.symbol)
        if st is None or st.proposal.direction != p.direction:
            to_close.append(p)
    return to_open, to_close


def open_position(
    st: SizedTrade, cycle: int, ts: datetime, slippage_bps: float,
    decision_id: str | None = None, pay_bnb: bool = False,
) -> tuple[Position, float]:
    """Open a position at a slipped entry fill; returns (Position, entry_fee_usdt)."""
    p = st.proposal
    entry_fill = fill_price(p.entry, open_side(p.direction), slippage_bps)
    entry_fee = trade_fee(st.qty * entry_fill, maker=False, pay_bnb=pay_bnb)
    position = Position(
        symbol=p.symbol, direction=p.direction, qty=st.qty, entry=entry_fill, stop=p.stop,
        take_profits=p.take_profits, leverage=st.leverage, margin=st.margin,
        liq_price=st.liq_price, opened_cycle=cycle, opened_ts=ts, decision_id=decision_id,
    )
    return position, entry_fee


def close_at_mark(
    position: Position, mark: float, *, funding_rate: float, funding_events: int,
    slippage_bps: float, pay_bnb: bool = False,
) -> ClosedTrade:
    """Discretionary close at the current mark (used when the team exits a position by decision
    rather than a stop/tp/liq trigger). Reason is recorded as 'close' for discretionary exits."""
    side = close_side(position.direction)
    exit_fill = fill_price(mark, side, slippage_bps)
    if position.direction == "long":
        gross = position.qty * (exit_fill - position.entry)
    else:
        gross = position.qty * (position.entry - exit_fill)
    exit_fee = trade_fee(position.qty * exit_fill, maker=False, pay_bnb=pay_bnb)
    # Signed funding: positive = we PAID it (reduces PnL), negative = we RECEIVED a credit
    # (raises PnL). Do NOT clamp to 0 — that drops carry credits on funding-receiving trades.
    funding = project_funding(position.qty * position.entry, funding_rate,
                              position.direction, funding_events)
    slippage = abs(exit_fill - mark) * position.qty
    return ClosedTrade(
        symbol=position.symbol, direction=position.direction, decision_id=position.decision_id,
        entry=position.entry, exit_price=exit_fill, qty=position.qty, reason="close",
        gross_pnl=gross, exit_fee=exit_fee, funding=funding, slippage=slippage,
        realized_pnl=gross - exit_fee - funding,
    )
