from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

SentimentLevel = Literal[
    "very_positive", "positive", "neutral", "negative", "very_negative"
]

# Canonical anchors for the ordinal level <-> numeric s mapping (§7.1). The five
# levels map onto {+2..-2}/2, i.e. evenly spaced on [-1, 1].
LEVEL_TO_S: dict[SentimentLevel, float] = {
    "very_positive": 1.0,
    "positive": 0.5,
    "neutral": 0.0,
    "negative": -0.5,
    "very_negative": -1.0,
}


def level_to_s(level: SentimentLevel) -> float:
    """Ordinal level -> numeric s in [-1, 1] ({+2..-2}/2). Enforces the §7.1 mapping."""
    return LEVEL_TO_S[level]


def s_to_level(s: float) -> SentimentLevel:
    """Inverse bucketing of `s` back to an ordinal level.

    Round-trips the canonical anchors (-1, -0.5, 0, 0.5, 1) exactly: each anchor sits
    in the middle of its 0.5-wide bucket. The boundaries (±0.25, ±0.75) are placed so
    that s_to_level(level_to_s(level)) == level for every level.
    """
    if s >= 0.75:
        return "very_positive"
    if s >= 0.25:
        return "positive"
    if s > -0.25:
        return "neutral"
    if s > -0.75:
        return "negative"
    return "very_negative"


def decay_score(s: float, age_hours: float, half_life_days: float = 2.5) -> float:
    """Exponential decay of a sentiment score toward 0 as it ages.

    s * 0.5 ** (age_hours / (half_life_days * 24)). At age 0 the score is unchanged;
    after one half-life it is halved; as age -> inf it tends to 0. A non-positive
    half-life disables decay (returns `s` unchanged) to avoid division/➗ blow-ups.
    """
    if half_life_days <= 0:
        return s
    return s * (0.5 ** (age_hours / (half_life_days * 24.0)))


def _parse_published(published_at: object) -> datetime | None:
    """Parse a source's published timestamp into a tz-aware UTC datetime.

    Real RSS `<pubDate>` is RFC-822 (e.g. 'Fri, 29 May 2026 14:20:32 +0000'); Atom and
    some feeds emit ISO-8601. Try ISO first, then RFC-822. A naive result is assumed
    UTC; an aware result is normalised to UTC. Returns None when the value is empty or
    unparseable (tolerates junk — never raises).
    """
    if not published_at:
        return None
    s = str(published_at).strip()
    if not s:
        return None
    for parse in (datetime.fromisoformat, parsedate_to_datetime):
        try:
            dt = parse(s)
        except (TypeError, ValueError):
            continue
        if dt is None:
            continue
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    return None


def validate_point_in_time(item_ts: datetime, as_of_ts: datetime) -> bool:
    """True iff `item_ts` is strictly before `as_of_ts` (no post-decision leakage).

    Both timestamps are compared as tz-aware UTC; a naive input is assumed UTC. An item
    published at or after the decision-time anchor fails the check.
    """
    item = item_ts if item_ts.tzinfo is not None else item_ts.replace(tzinfo=UTC)
    as_of = as_of_ts if as_of_ts.tzinfo is not None else as_of_ts.replace(tzinfo=UTC)
    return item < as_of
