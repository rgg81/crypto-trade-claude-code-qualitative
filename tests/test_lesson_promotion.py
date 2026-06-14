from datetime import UTC, datetime

from futures_fund.lessons import (
    append_lesson,
    confirm_lesson,
    demote_lesson,
    read_lessons,
    retire_lesson,
    update_lesson,
    validated_lessons,
)


def _add(tmp_path, **over):
    base = dict(text="cut leverage in chop", regime="high_vol_range", tags=["vol"], importance=6)
    base.update(over)
    return append_lesson(tmp_path, base, ts=datetime(2026, 5, 1, tzinfo=UTC))


def test_update_lesson_rewrites_field(tmp_path):
    lid = _add(tmp_path)
    assert update_lesson(tmp_path, lid, importance=9) is True
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).importance == 9
    assert update_lesson(tmp_path, "missing", importance=1) is False


def test_confirm_promotes_candidate_at_threshold(tmp_path):
    lid = _add(tmp_path)
    for _ in range(4):
        confirm_lesson(tmp_path, lid, promote_threshold=5)
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "candidate"
    confirm_lesson(tmp_path, lid, promote_threshold=5)  # 5th confirmation
    lz = next(lz for lz in read_lessons(tmp_path) if lz.id == lid)
    assert lz.state == "validated" and lz.confirmations == 5


def test_validated_lessons_are_the_veto_set(tmp_path):
    a = _add(tmp_path, text="A")
    _add(tmp_path, text="B")  # stays candidate
    for _ in range(5):
        confirm_lesson(tmp_path, a, promote_threshold=5)
    vals = validated_lessons(tmp_path)
    assert [lz.text for lz in vals] == ["A"]


def test_demote_steps_down_then_retires(tmp_path):
    lid = _add(tmp_path)
    for _ in range(5):
        confirm_lesson(tmp_path, lid, promote_threshold=5)  # -> validated
    assert demote_lesson(tmp_path, lid) is True  # validated -> candidate
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "candidate"
    assert demote_lesson(tmp_path, lid) is True  # candidate -> retired
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "retired"


def test_retire_lesson(tmp_path):
    lid = _add(tmp_path)
    assert retire_lesson(tmp_path, lid) is True
    assert next(lz for lz in read_lessons(tmp_path) if lz.id == lid).state == "retired"


def test_demote_resets_confirmations_so_repromotion_is_not_instant(tmp_path):
    lid = _add(tmp_path)
    for _ in range(5):
        confirm_lesson(tmp_path, lid, promote_threshold=5)  # -> validated, confirmations 5
    demote_lesson(tmp_path, lid)  # validated -> candidate, confirmations reset to 0
    lz = next(z for z in read_lessons(tmp_path) if z.id == lid)
    assert lz.state == "candidate" and lz.confirmations == 0
    confirm_lesson(tmp_path, lid, promote_threshold=5)  # a single confirm must NOT re-promote
    assert next(z for z in read_lessons(tmp_path) if z.id == lid).state == "candidate"
