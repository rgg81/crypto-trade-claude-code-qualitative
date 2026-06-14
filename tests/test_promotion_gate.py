from datetime import UTC, datetime

from futures_fund.lessons import append_lesson, read_lessons, statistically_promote


def _add(tmp_path):
    return append_lesson(tmp_path, {"text": "x", "regime": "high_vol_trend", "tags": ["t"],
                                    "confirmations": 4}, ts=datetime(2026, 5, 1, tzinfo=UTC))


def test_promote_blocked_when_edge_not_significant(tmp_path):
    lid = _add(tmp_path)
    # 5th confirmation would hit threshold, but DSR below gate -> stays candidate
    statistically_promote(tmp_path, lid, dsr_pvalue=0.5, promote_threshold=5)
    assert next(z for z in read_lessons(tmp_path) if z.id == lid).state == "candidate"


def test_promote_allowed_when_edge_significant(tmp_path):
    lid = _add(tmp_path)
    statistically_promote(tmp_path, lid, dsr_pvalue=0.97, promote_threshold=5)
    lz = next(z for z in read_lessons(tmp_path) if z.id == lid)
    assert lz.state == "validated" and lz.confirmations == 5
