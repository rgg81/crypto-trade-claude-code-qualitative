from datetime import UTC, datetime

import pytest

from futures_fund.equity_log import period_return, record_equity


def test_period_return_uses_baseline_before_cutoff(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 2, tzinfo=UTC), 10_300.0, cycle=2)  # +3% over 1 day
    now = datetime(2026, 5, 2, tzinfo=UTC)
    assert period_return(tmp_path, now, days=1) == pytest.approx(0.03)


def test_period_return_negative_drawdown(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 12, tzinfo=UTC), 9_600.0, cycle=2)  # -4%
    now = datetime(2026, 5, 1, 12, tzinfo=UTC)
    assert period_return(tmp_path, now, days=1) == pytest.approx(-0.04)


def test_period_return_too_little_history_is_zero(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    assert period_return(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), days=1) == 0.0
