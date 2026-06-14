from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from futures_fund.sentiment_decay import (
    LEVEL_TO_S,
    SentimentLevel,
    _parse_published,
    decay_score,
    level_to_s,
    s_to_level,
    validate_point_in_time,
)

LEVELS: list[SentimentLevel] = [
    "very_positive", "positive", "neutral", "negative", "very_negative"
]


# --- level <-> s round-trips -------------------------------------------------

@pytest.mark.parametrize("level", LEVELS)
def test_level_to_s_to_level_round_trip(level: SentimentLevel) -> None:
    assert s_to_level(level_to_s(level)) == level


@pytest.mark.parametrize("level,expected", list(LEVEL_TO_S.items()))
def test_level_to_s_anchors(level: SentimentLevel, expected: float) -> None:
    assert level_to_s(level) == expected


def test_s_to_level_anchors_exact() -> None:
    # The canonical anchors map back to their own level.
    assert s_to_level(1.0) == "very_positive"
    assert s_to_level(0.5) == "positive"
    assert s_to_level(0.0) == "neutral"
    assert s_to_level(-0.5) == "negative"
    assert s_to_level(-1.0) == "very_negative"


def test_s_to_level_bucket_boundaries() -> None:
    # Boundaries are placed so every anchor is centred in its bucket.
    assert s_to_level(0.75) == "very_positive"
    assert s_to_level(0.74) == "positive"
    assert s_to_level(0.25) == "positive"
    assert s_to_level(0.24) == "neutral"
    assert s_to_level(-0.24) == "neutral"
    assert s_to_level(-0.25) == "negative"
    assert s_to_level(-0.74) == "negative"
    assert s_to_level(-0.75) == "very_negative"


# --- decay -------------------------------------------------------------------

def test_decay_at_age_zero_is_unchanged() -> None:
    assert decay_score(0.8, 0.0) == pytest.approx(0.8)


def test_decay_one_half_life_halves() -> None:
    # default half-life is 2.5 days = 60 hours
    assert decay_score(1.0, 2.5 * 24.0) == pytest.approx(0.5)
    assert decay_score(1.0, 2.5 * 24.0, half_life_days=2.5) == pytest.approx(0.5)


def test_decay_two_half_lives_quarters() -> None:
    assert decay_score(1.0, 2.0 * 2.5 * 24.0) == pytest.approx(0.25)


def test_decay_strictly_decreases_with_age_positive() -> None:
    s = 0.9
    ages = [0.0, 1.0, 6.0, 24.0, 100.0, 1000.0]
    vals = [decay_score(s, a) for a in ages]
    for prev, cur in zip(vals, vals[1:]):
        assert cur < prev


def test_decay_magnitude_strictly_decreases_with_age_negative() -> None:
    s = -0.9
    ages = [0.0, 1.0, 6.0, 24.0, 100.0, 1000.0]
    vals = [decay_score(s, a) for a in ages]
    for prev, cur in zip(vals, vals[1:]):
        # negative score decays UP toward 0, magnitude shrinks
        assert abs(cur) < abs(prev)
        assert cur > prev


def test_decay_tends_to_zero_as_age_to_inf() -> None:
    assert decay_score(1.0, 1e6) == pytest.approx(0.0, abs=1e-9)
    assert decay_score(-1.0, 1e6) == pytest.approx(0.0, abs=1e-9)


def test_decay_preserves_sign() -> None:
    assert decay_score(0.5, 50.0) > 0
    assert decay_score(-0.5, 50.0) < 0


def test_decay_non_positive_half_life_disables_decay() -> None:
    assert decay_score(0.7, 999.0, half_life_days=0.0) == 0.7
    assert decay_score(0.7, 999.0, half_life_days=-1.0) == 0.7


def test_decay_zero_stays_zero() -> None:
    assert decay_score(0.0, 123.0) == 0.0


# --- point-in-time -----------------------------------------------------------

AS_OF = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def test_point_in_time_before_passes() -> None:
    assert validate_point_in_time(AS_OF - timedelta(seconds=1), AS_OF) is True


def test_point_in_time_equal_fails() -> None:
    # strictly-before: item AT the anchor leaks the decision moment
    assert validate_point_in_time(AS_OF, AS_OF) is False


def test_point_in_time_after_fails() -> None:
    assert validate_point_in_time(AS_OF + timedelta(seconds=1), AS_OF) is False


def test_point_in_time_naive_item_assumed_utc() -> None:
    naive_before = datetime(2026, 6, 13, 11, 0, 0)
    assert validate_point_in_time(naive_before, AS_OF) is True


def test_point_in_time_cross_timezone() -> None:
    # 11:00 in UTC-1 == 12:00 UTC == the anchor -> not strictly before
    item = datetime(2026, 6, 13, 11, 0, 0, tzinfo=timezone(timedelta(hours=-1)))
    assert validate_point_in_time(item, AS_OF) is False
    earlier = datetime(2026, 6, 13, 10, 59, 0, tzinfo=timezone(timedelta(hours=-1)))
    assert validate_point_in_time(earlier, AS_OF) is True


# --- _parse_published --------------------------------------------------------

def test_parse_iso_with_offset() -> None:
    dt = _parse_published("2026-05-29T14:20:32+00:00")
    assert dt == datetime(2026, 5, 29, 14, 20, 32, tzinfo=UTC)
    assert dt.tzinfo is not None


def test_parse_iso_with_z_suffix() -> None:
    dt = _parse_published("2026-05-29T14:20:32Z")
    assert dt == datetime(2026, 5, 29, 14, 20, 32, tzinfo=UTC)


def test_parse_iso_naive_assumed_utc() -> None:
    dt = _parse_published("2026-05-29T14:20:32")
    assert dt == datetime(2026, 5, 29, 14, 20, 32, tzinfo=UTC)
    assert dt.tzinfo is UTC


def test_parse_iso_offset_normalised_to_utc() -> None:
    dt = _parse_published("2026-05-29T16:20:32+02:00")
    assert dt == datetime(2026, 5, 29, 14, 20, 32, tzinfo=UTC)
    assert dt.utcoffset() == timedelta(0)


def test_parse_rfc822() -> None:
    dt = _parse_published("Fri, 29 May 2026 14:20:32 +0000")
    assert dt == datetime(2026, 5, 29, 14, 20, 32, tzinfo=UTC)


def test_parse_rfc822_with_offset_normalised() -> None:
    dt = _parse_published("Fri, 29 May 2026 16:20:32 +0200")
    assert dt == datetime(2026, 5, 29, 14, 20, 32, tzinfo=UTC)


@pytest.mark.parametrize("garbage", [
    "", "   ", None, "not a date", "garbage 123", "2026-13-45T99:99:99",
    [], {}, "Mon, 99 Xxx 9999 99:99:99 +9999",
])
def test_parse_garbage_returns_none(garbage: object) -> None:
    assert _parse_published(garbage) is None


def test_parse_result_always_aware_when_parsed() -> None:
    for val in ("2026-05-29T14:20:32", "Fri, 29 May 2026 14:20:32 +0000"):
        dt = _parse_published(val)
        assert dt is not None
        assert dt.tzinfo is not None
        assert dt.utcoffset() == timedelta(0)
