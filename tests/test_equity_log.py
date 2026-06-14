from datetime import UTC, datetime

import pytest

from futures_fund.equity_log import equity_series, record_equity, returns_series


def test_record_and_series_roundtrip(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 10_000.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 4, tzinfo=UTC), 10_100.0, cycle=2)
    series = equity_series(tmp_path)
    assert [e for _, e in series] == [10_000.0, 10_100.0]


def test_returns_series_is_pct_change(tmp_path):
    record_equity(tmp_path, datetime(2026, 5, 1, tzinfo=UTC), 100.0, cycle=1)
    record_equity(tmp_path, datetime(2026, 5, 1, 4, tzinfo=UTC), 110.0, cycle=2)
    record_equity(tmp_path, datetime(2026, 5, 1, 8, tzinfo=UTC), 99.0, cycle=3)
    rets = returns_series(tmp_path)
    assert rets[0] == pytest.approx(0.10)
    assert rets[1] == pytest.approx(-0.10)


def test_empty_series(tmp_path):
    assert equity_series(tmp_path) == []
    assert returns_series(tmp_path) == []
