from datetime import UTC, datetime

from futures_fund.state import (
    AccountState,
    Position,
    is_halted,
    load_account,
    load_positions,
    save_account,
    save_positions,
    set_halt,
)


def _pos(symbol="BTCUSDT", direction="long"):
    stop = 95.0 if direction == "long" else 105.0
    return Position(
        symbol=symbol, direction=direction, qty=0.5, entry=100.0, stop=stop,
        take_profits=[115.0], leverage=5.0, margin=10.0, liq_price=82.0,
        opened_cycle=1, opened_ts=datetime(2026, 5, 29, tzinfo=UTC),
        decision_id="abc",
    )


def test_load_account_defaults_when_absent(tmp_path):
    acct = load_account(tmp_path, default_balance=10_000.0)
    assert acct.balance == 10_000.0
    assert acct.peak_equity == 10_000.0
    assert acct.halt is False


def test_save_then_load_account_roundtrip(tmp_path):
    save_account(tmp_path, AccountState(balance=12_345.0, peak_equity=13_000.0))
    acct = load_account(tmp_path, default_balance=10_000.0)
    assert acct.balance == 12_345.0
    assert acct.peak_equity == 13_000.0


def test_positions_roundtrip(tmp_path):
    save_positions(tmp_path, [_pos(), _pos("ETHUSDT", "short")])
    loaded = load_positions(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].symbol == "BTCUSDT" and loaded[0].direction == "long"
    assert loaded[1].direction == "short"
    assert str(loaded[0].opened_ts.tzinfo) == "UTC"


def test_load_positions_empty_when_absent(tmp_path):
    assert load_positions(tmp_path) == []


def test_halt_flag_set_and_read(tmp_path):
    load_account(tmp_path, default_balance=10_000.0)  # ensure file
    assert is_halted(tmp_path) is False
    set_halt(tmp_path, True, reason="manual kill")
    assert is_halted(tmp_path) is True
    set_halt(tmp_path, False)
    assert is_halted(tmp_path) is False
