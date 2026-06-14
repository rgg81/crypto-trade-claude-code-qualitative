from __future__ import annotations

from futures_fund.models import Direction, MmrBracket


def mmr_for_notional(notional: float, brackets: list[MmrBracket]) -> tuple[float, float]:
    """Return (mmr, maint_amount) for the bracket containing `notional`.

    Brackets are sorted by floor; if notional exceeds the top cap, use the top bracket.
    """
    notional = abs(notional)
    ordered = sorted(brackets, key=lambda b: b.notional_floor)
    chosen = ordered[0]
    for b in ordered:
        if notional >= b.notional_floor:
            chosen = b
        else:
            break
    return chosen.mmr, chosen.maint_amount


def liquidation_price(
    entry: float, qty: float, margin: float, direction: Direction,
    mmr: float, maint_amount: float,
) -> float:
    """Isolated-margin liquidation price for a single position.

    Long:  (qty*entry - margin - maint_amount) / (qty*(1 - mmr))
    Short: (qty*entry + margin + maint_amount) / (qty*(1 + mmr))

    Matches Binance's USD-M formula (maintenance_margin = notional*mmr - maint_amount);
    verified symbolically. Assumes (mmr, maint_amount) come from the bracket of the ENTRY
    notional; if the resulting liq price implies a different bracket, A3 must re-solve with
    that bracket's values. The live liquidation TRIGGER compares MARK price to this value.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    notional_at_entry = qty * entry
    if direction == "long":
        return (notional_at_entry - margin - maint_amount) / (qty * (1.0 - mmr))
    return (notional_at_entry + margin + maint_amount) / (qty * (1.0 + mmr))
