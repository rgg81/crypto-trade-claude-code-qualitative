"""D: the lessons corpus must not be a one-way (restrictive-only) ratchet. A `polarity` field
plus a retrieval quota force-include >=1 enabling lesson and cap restrictive injections, so a
losing record can no longer monopolize every debate with prohibitions."""
from datetime import UTC, datetime, timedelta

from futures_fund.lessons import Lesson, append_lesson, retrieve_lessons


def _L(text, polarity="restrictive", regime=None, tags=("risk",), importance=5, state="candidate"):
    return {"text": text, "polarity": polarity, "regime": regime, "tags": list(tags),
            "importance": importance, "state": state}


def test_polarity_defaults_restrictive_for_legacy_lessons():
    # a lesson written before the field existed loads as 'restrictive' (the dominant legacy type)
    lz = Lesson(ts=datetime(2026, 5, 1, tzinfo=UTC), text="legacy")
    assert lz.polarity == "restrictive"


def test_polarity_roundtrips(tmp_path):
    now = datetime(2026, 5, 1, tzinfo=UTC)
    append_lesson(tmp_path, _L("take the squeeze long", polarity="enabling"), ts=now)
    from futures_fund.lessons import read_lessons
    assert read_lessons(tmp_path)[0].polarity == "enabling"


def test_retrieve_force_includes_one_enabling(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    # 4 high-importance recent restrictive (high score) + 1 stale low-importance enabling (low score)
    for i in range(4):
        append_lesson(tmp_path, _L(f"dont{i}", "restrictive", importance=9, tags=["trend"]),
                      ts=now - timedelta(hours=1))
    append_lesson(tmp_path, _L("DO take crowded-short squeeze longs", "enabling",
                               importance=4, tags=["trend"]), ts=now - timedelta(hours=80))
    got = retrieve_lessons(tmp_path, now=now, regime="trend", query_tags=["trend"], k=5)
    assert any(lz.polarity == "enabling" for lz in got), [lz.text for lz in got]


def test_retrieve_caps_restrictive_injections(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    for i in range(6):
        append_lesson(tmp_path, _L(f"dont{i}", "restrictive", importance=9, tags=["risk"]),
                      ts=now - timedelta(hours=1))
    got = retrieve_lessons(tmp_path, now=now, regime="x", query_tags=["risk"], k=5)
    assert sum(1 for lz in got if lz.polarity == "restrictive") <= 3


def test_retrieve_never_drops_a_validated_lesson(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    append_lesson(tmp_path, _L("VALIDATED hard veto", "restrictive", importance=9,
                               tags=["risk"], state="validated"), ts=now - timedelta(hours=1))
    for i in range(5):
        append_lesson(tmp_path, _L(f"cand{i}", "restrictive", importance=9, tags=["risk"]),
                      ts=now - timedelta(hours=1))
    got = retrieve_lessons(tmp_path, now=now, regime="x", query_tags=["risk"], k=3)
    assert any(lz.state == "validated" for lz in got)
