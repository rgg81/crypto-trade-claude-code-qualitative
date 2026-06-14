from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.models import Direction


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace (atomic rename). A crash mid-write leaves the PRIOR file
    intact rather than a half-written one — so account/positions can never be corrupted into a
    permanent-RETRY wedge (the same pattern cycle_io/regime/pending_orders already use)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class Position(BaseModel):
    symbol: str
    direction: Direction
    qty: float
    entry: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    leverage: float
    margin: float
    liq_price: float
    opened_cycle: int
    opened_ts: datetime
    decision_id: str | None = None

    # NOTE: a Position's `stop` is NOT constrained to the loss side of entry. At OPEN the stop is
    # loss-side (enforced on TradeProposal), but a winner's stop may be TRAILED past entry to lock
    # profit (long stop > entry, short stop < entry). The trail step keeps it tighten-only and
    # short of the current mark; downside risk of a profit-locked stop is 0 (see position_risk).


class AccountState(BaseModel):
    balance: float          # realized USDT wallet balance
    peak_equity: float      # peak of total equity (balance + unrealized) ever seen
    halt: bool = False
    halt_reason: str = ""
    updated_ts: datetime | None = None


def _account_path(state_dir) -> Path:
    return Path(state_dir) / "account.json"


def _positions_path(state_dir) -> Path:
    return Path(state_dir) / "positions.json"


def load_account(state_dir, default_balance: float) -> AccountState:
    p = _account_path(state_dir)
    if p.exists():
        return AccountState.model_validate_json(p.read_text())
    return AccountState(balance=default_balance, peak_equity=default_balance)


def save_account(state_dir, account: AccountState) -> None:
    _atomic_write_text(_account_path(state_dir), account.model_dump_json(indent=2))


def load_positions(state_dir) -> list[Position]:
    p = _positions_path(state_dir)
    if not p.exists():
        return []
    raw = json.loads(p.read_text())
    return [Position.model_validate(r) for r in raw]


def save_positions(state_dir, positions: list[Position]) -> None:
    _atomic_write_text(_positions_path(state_dir),
                       json.dumps([json.loads(pos.model_dump_json()) for pos in positions], indent=2))


def is_halted(state_dir) -> bool:
    p = _account_path(state_dir)
    if not p.exists():
        return False
    return AccountState.model_validate_json(p.read_text()).halt


def set_halt(state_dir, halt: bool, reason: str = "") -> None:
    # operates on the persisted account; balance/peak default to 0 only if no account exists yet
    p = _account_path(state_dir)
    acct = (
        AccountState.model_validate_json(p.read_text())
        if p.exists()
        else AccountState(balance=0.0, peak_equity=0.0)
    )
    acct.halt = halt
    acct.halt_reason = reason if halt else ""
    save_account(state_dir, acct)
