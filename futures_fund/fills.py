from __future__ import annotations

from futures_fund.models import Direction


def open_side(direction: Direction) -> str:
    return "buy" if direction == "long" else "sell"


def close_side(direction: Direction) -> str:
    return "sell" if direction == "long" else "buy"


def fill_price(reference_price: float, side: str, slippage_bps: float) -> float:
    """Apply paper slippage to a reference price. Buys slip up, sells slip down."""
    adj = slippage_bps / 10_000.0
    return reference_price * (1.0 + adj) if side == "buy" else reference_price * (1.0 - adj)
