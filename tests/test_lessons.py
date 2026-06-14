from datetime import UTC, datetime, timedelta

from futures_fund.lessons import (
    Lesson,
    append_lesson,
    read_lessons,
    retrieve_lessons,
    score_lesson,
)


def _lesson(**over):
    base = dict(text="don't fight strong funding", regime="high_vol_trend",
                symbol="BTCUSDT", tags=["funding", "trend"], importance=8)
    base.update(over)
    return base


def test_append_returns_id_and_read_roundtrip(tmp_path):
    lid = append_lesson(tmp_path, _lesson(), ts=datetime(2026, 5, 1, tzinfo=UTC))
    lessons = read_lessons(tmp_path)
    assert len(lessons) == 1 and lessons[0].id == lid
    assert lessons[0].state == "candidate" and lessons[0].importance == 8


def test_score_combines_recency_importance_relevance():
    now = datetime(2026, 5, 2, tzinfo=UTC)
    recent = Lesson(id="a", ts=now - timedelta(hours=1), text="x", importance=10,
                    tags=["funding"])
    old = Lesson(id="b", ts=now - timedelta(hours=500), text="y", importance=10,
                 tags=["funding"])
    # same importance & relevance; the recent one must score higher
    assert score_lesson(recent, now, ["funding"]) > score_lesson(old, now, ["funding"])
    # tag overlap raises relevance
    s_match = score_lesson(recent, now, ["funding"])
    s_nomatch = score_lesson(recent, now, ["macro"])
    assert s_match > s_nomatch


def test_retrieve_filters_by_regime_then_ranks_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    append_lesson(tmp_path, _lesson(text="trend lesson", regime="high_vol_trend",
                                    tags=["trend"]), ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="range lesson", regime="low_vol_range",
                                    tags=["meanrev"]), ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="universal", regime=None, tags=["risk"]),
                  ts=now - timedelta(hours=2))
    got = retrieve_lessons(tmp_path, now=now, regime="high_vol_trend",
                           query_tags=["trend"], k=5)
    texts = [lz.text for lz in got]
    assert "trend lesson" in texts        # matching regime
    assert "universal" in texts           # regime=None applies everywhere
    assert "range lesson" not in texts    # wrong regime filtered out


def test_retrieve_matches_engine_label_and_quadrant_and_any(tmp_path):
    # cy77/78 retrospective P0: 50 lessons are tagged with the ENGINE label ('risk_off') and 11 with
    # 'any', but SKILL passes the symbol QUADRANT ('high_vol_trend') as the query — so they were all
    # STRANDED. Retrieval must accept BOTH contexts (quadrant + engine label) and treat 'any' as
    # universal, so a risk_off edge lesson surfaces in a risk_off cycle regardless of quadrant.
    now = datetime(2026, 5, 2, tzinfo=UTC)
    append_lesson(tmp_path, _lesson(text="risk_off edge", regime="risk_off", tags=["flush"]),
                  ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="quadrant lesson", regime="high_vol_trend", tags=["t"]),
                  ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="any lesson", regime="any", tags=["x"]),
                  ts=now - timedelta(hours=2))
    append_lesson(tmp_path, _lesson(text="risk_on only", regime="risk_on", tags=["y"]),
                  ts=now - timedelta(hours=2))
    # query carries BOTH the engine label and the symbol quadrant
    got = retrieve_lessons(tmp_path, now=now, regime=["risk_off", "high_vol_trend"],
                           query_tags=["flush"], k=10)
    texts = [lz.text for lz in got]
    assert "risk_off edge" in texts        # engine-label match (was stranded before)
    assert "quadrant lesson" in texts      # quadrant match
    assert "any lesson" in texts           # 'any' is universal (was stranded before)
    assert "risk_on only" not in texts     # a non-matching desk regime is still excluded
    # a single-string regime still works (back-compat)
    single = retrieve_lessons(tmp_path, now=now, regime="risk_off", query_tags=["flush"], k=10)
    assert "risk_off edge" in [lz.text for lz in single]
    assert "quadrant lesson" not in [lz.text for lz in single]   # only the matched context


def test_retrieve_respects_top_k(tmp_path):
    now = datetime(2026, 5, 2, tzinfo=UTC)
    for i in range(10):
        append_lesson(tmp_path, _lesson(text=f"l{i}", regime=None, tags=["risk"]),
                      ts=now - timedelta(hours=i + 1))
    assert len(retrieve_lessons(tmp_path, now=now, regime="x", query_tags=["risk"], k=3)) == 3
