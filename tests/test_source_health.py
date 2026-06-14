from __future__ import annotations

from datetime import UTC, datetime, timedelta

from futures_fund.source_health import (
    SourceHealth,
    degraded_sources,
    healthy_sources,
    is_healthy,
    load_health,
    record_err,
    record_ok,
    save_health,
)

T0 = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def test_unknown_source_healthy_by_default():
    h = SourceHealth()
    assert is_healthy(h, "rss:coindesk", T0) is True
    assert healthy_sources(h, ["a", "b"], T0) == ["a", "b"]
    assert degraded_sources(h, T0) == set()


def test_record_ok_stamps_stats():
    h = SourceHealth()
    record_ok(h, "rss:coindesk", latency_ms=123.5, now=T0)
    s = h.sources["rss:coindesk"]
    assert s.total_ok == 1
    assert s.consecutive_failures == 0
    assert s.last_latency_ms == 123.5
    assert s.last_ok_ts == T0.isoformat()
    assert s.disabled_until is None


def test_below_threshold_stays_healthy():
    h = SourceHealth()
    record_err(h, "src", now=T0, k_threshold=3)
    record_err(h, "src", now=T0, k_threshold=3)
    s = h.sources["src"]
    assert s.consecutive_failures == 2
    assert s.total_err == 2
    assert s.disabled_until is None
    assert is_healthy(h, "src", T0) is True


def test_k_consecutive_errors_trips_circuit_breaker():
    h = SourceHealth()
    for _ in range(3):
        record_err(h, "src", now=T0, k_threshold=3, base_backoff_min=15)
    assert is_healthy(h, "src", T0) is False
    assert "src" in degraded_sources(h, T0)
    # disabled for base_backoff_min (2**0 == 1) -> 15 minutes from now
    until = datetime.fromisoformat(h.sources["src"].disabled_until)
    assert until == T0 + timedelta(minutes=15)


def test_recovers_after_disabled_until_passes():
    h = SourceHealth()
    for _ in range(3):
        record_err(h, "src", now=T0, k_threshold=3, base_backoff_min=15)
    assert is_healthy(h, "src", T0) is False
    just_before = T0 + timedelta(minutes=14, seconds=59)
    just_after = T0 + timedelta(minutes=15)
    assert is_healthy(h, "src", just_before) is False
    assert is_healthy(h, "src", just_after) is True
    assert "src" not in degraded_sources(h, just_after)


def test_single_ok_resets_consecutive_failures_and_lifts_breaker():
    h = SourceHealth()
    for _ in range(4):
        record_err(h, "src", now=T0, k_threshold=3, base_backoff_min=15)
    assert is_healthy(h, "src", T0) is False
    record_ok(h, "src", latency_ms=50.0, now=T0 + timedelta(minutes=20))
    s = h.sources["src"]
    assert s.consecutive_failures == 0
    assert s.disabled_until is None
    assert s.total_err == 4  # error tally is cumulative, not reset
    assert s.total_ok == 1
    assert is_healthy(h, "src", T0 + timedelta(minutes=20)) is True


def test_backoff_grows_exponentially():
    h = SourceHealth()
    # first trip at failure 3 -> 15 * 2**0 = 15 min
    for _ in range(3):
        record_err(h, "src", now=T0, k_threshold=3, base_backoff_min=15)
    until_3 = datetime.fromisoformat(h.sources["src"].disabled_until)
    assert until_3 - T0 == timedelta(minutes=15)
    # failure 4 -> 15 * 2**1 = 30 min
    record_err(h, "src", now=T0, k_threshold=3, base_backoff_min=15)
    until_4 = datetime.fromisoformat(h.sources["src"].disabled_until)
    assert until_4 - T0 == timedelta(minutes=30)
    # failure 5 -> 15 * 2**2 = 60 min
    record_err(h, "src", now=T0, k_threshold=3, base_backoff_min=15)
    until_5 = datetime.fromisoformat(h.sources["src"].disabled_until)
    assert until_5 - T0 == timedelta(minutes=60)


def test_healthy_sources_filters_degraded():
    h = SourceHealth()
    for _ in range(3):
        record_err(h, "bad", now=T0, k_threshold=3, base_backoff_min=15)
    record_ok(h, "good", latency_ms=10.0, now=T0)
    assert healthy_sources(h, ["good", "bad", "unknown"], T0) == ["good", "unknown"]


def test_load_defaults_when_absent(tmp_path):
    h = load_health(tmp_path)
    assert h.sources == {}


def test_save_then_load_roundtrip(tmp_path):
    h = SourceHealth()
    record_ok(h, "good", latency_ms=42.0, now=T0)
    for _ in range(3):
        record_err(h, "bad", now=T0, k_threshold=3, base_backoff_min=15)
    save_health(tmp_path, h)
    # written under state/crawl/source_health.json
    assert (tmp_path / "crawl" / "source_health.json").exists()
    loaded = load_health(tmp_path)
    assert loaded.sources["good"].last_latency_ms == 42.0
    assert loaded.sources["good"].total_ok == 1
    assert loaded.sources["bad"].consecutive_failures == 3
    assert is_healthy(loaded, "bad", T0) is False
    assert is_healthy(loaded, "good", T0) is True


def test_load_corrupt_file_returns_empty(tmp_path):
    p = tmp_path / "crawl" / "source_health.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert load_health(tmp_path).sources == {}
