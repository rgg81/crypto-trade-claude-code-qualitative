"""P1 fix: account/positions saves must be ATOMIC (tmp + os.replace), so a crash mid-write leaves
the PRIOR valid file intact instead of a half-written one that wedges every subsequent cycle into a
permanent-RETRY parse failure."""
from datetime import UTC, datetime

import pytest

from futures_fund import state as st
from futures_fund.state import (
    AccountState,
    Position,
    load_account,
    load_positions,
    save_account,
    save_positions,
)

_TS = datetime(2026, 3, 1, tzinfo=UTC)


def _pos():
    return Position(symbol="BTCUSDT", direction="long", qty=0.1, entry=100.0, stop=95.0,
                    take_profits=[110.0], leverage=2.0, margin=5.0, liq_price=50.0,
                    opened_cycle=1, opened_ts=_TS)


def test_account_save_round_trips(tmp_path):
    save_account(tmp_path, AccountState(balance=9876.5, peak_equity=10_000.0))
    assert load_account(tmp_path, 0.0).balance == 9876.5


def test_positions_save_round_trips(tmp_path):
    save_positions(tmp_path, [_pos()])
    out = load_positions(tmp_path)
    assert len(out) == 1 and out[0].symbol == "BTCUSDT"


def test_failed_account_write_preserves_prior_file(tmp_path, monkeypatch):
    # write a GOOD account, then simulate a crash DURING the next save's atomic rename
    save_account(tmp_path, AccountState(balance=10_000.0, peak_equity=10_000.0))

    def boom(*a, **k):
        raise OSError("disk full mid-rename")
    monkeypatch.setattr(st.os, "replace", boom)
    with pytest.raises(OSError):
        save_account(tmp_path, AccountState(balance=5_000.0, peak_equity=10_000.0))
    monkeypatch.undo()
    # the ORIGINAL balance is intact and loadable — NOT corrupted/half-written
    assert load_account(tmp_path, 0.0).balance == 10_000.0


def test_failed_positions_write_preserves_prior_file(tmp_path, monkeypatch):
    save_positions(tmp_path, [_pos()])

    def boom(*a, **k):
        raise OSError("crash mid-rename")
    monkeypatch.setattr(st.os, "replace", boom)
    with pytest.raises(OSError):
        save_positions(tmp_path, [])  # tried to flatten the book; the write fails
    monkeypatch.undo()
    # the prior position survives — the desk does not silently "lose" it to a corrupt file
    out = load_positions(tmp_path)
    assert len(out) == 1 and out[0].symbol == "BTCUSDT"


def test_no_tmp_residue_after_successful_save(tmp_path):
    save_positions(tmp_path, [_pos()])
    save_account(tmp_path, AccountState(balance=1.0, peak_equity=1.0))
    assert not list(tmp_path.glob("*.tmp"))  # temp files renamed away, none left behind
