from datetime import UTC, datetime

from futures_fund.journal import (
    Decision,
    append_decision,
    append_partial_bank,
    journal_file,
    patch_outcome,
    read_all_decisions,
    read_open_decisions,
    realized_total,
)


def _decision(**over):
    base = dict(
        ts=datetime(2026, 5, 29, 12, tzinfo=UTC), cycle=1, symbol="BTCUSDT",
        direction="long", entry=100.0, stop=95.0, confidence=0.7,
        rationale="momentum breakout", dominant_signal="trend",
    )
    base.update(over)
    return base


def test_append_returns_id_and_writes_monthly_file(tmp_path):
    did = append_decision(tmp_path, _decision())
    assert isinstance(did, str) and did
    f = journal_file(tmp_path, datetime(2026, 5, 29, tzinfo=UTC))
    assert f.exists() and f.name == "journal-2026-05.jsonl"
    recs = read_all_decisions(tmp_path)
    assert len(recs) == 1 and recs[0]["id"] == did and recs[0]["symbol"] == "BTCUSDT"


def test_open_decisions_excludes_closed(tmp_path):
    d1 = append_decision(tmp_path, _decision(symbol="BTCUSDT"))
    append_decision(tmp_path, _decision(symbol="ETHUSDT"))
    assert len(read_open_decisions(tmp_path)) == 2
    ok = patch_outcome(tmp_path, d1, {
        "exit_ts": datetime(2026, 5, 30, tzinfo=UTC), "realized_pnl": 42.0,
        "fees": 1.0, "prediction_correct": True, "low_level_lesson": "read was right",
    })
    assert ok is True
    opens = read_open_decisions(tmp_path)
    assert len(opens) == 1 and opens[0]["symbol"] == "ETHUSDT"


def test_patch_merges_outcome_fields(tmp_path):
    did = append_decision(tmp_path, _decision())
    patch_outcome(tmp_path, did, {"realized_pnl": -10.0, "prediction_correct": False})
    rec = next(r for r in read_all_decisions(tmp_path) if r["id"] == did)
    assert rec["realized_pnl"] == -10.0
    assert rec["prediction_correct"] is False
    assert rec["rationale"] == "momentum breakout"  # Phase-1 field preserved


def test_partial_bank_is_captured_and_totals_reconstruct(tmp_path):
    # cy77/78 retrospective P0: a 50% scale-out banks cash that was NEVER journaled (cy22 SOL +$119
    # invisible -> best short understated 44%). A partial bank must attach to its parent decision so
    # read_all_decisions reconstructs total realized = sum(partial banks) + final close.
    did = append_decision(tmp_path, _decision(direction="short", entry=80.0, stop=82.0, size=10.0))
    ok = append_partial_bank(tmp_path, did, {"pnl": 119.45, "fees": 0.5, "funding": 0.1,
                                             "fraction": 0.5, "price": 68.0,
                                             "ts": datetime(2026, 5, 29, 16, tzinfo=UTC)})
    assert ok
    patch_outcome(tmp_path, did, {"exit_ts": datetime(2026, 5, 30, tzinfo=UTC),
                                  "realized_pnl": 152.21})   # the final-half close
    rec = next(d for d in read_all_decisions(tmp_path) if d["id"] == did)
    assert len(rec["partial_banks"]) == 1 and rec["partial_banks"][0]["pnl"] == 119.45
    assert realized_total(rec) == 119.45 + 152.21          # TRUE realized, not just the final half
    # a plain trade with no scale-out is unchanged
    d2 = append_decision(tmp_path, _decision(cycle=2))
    patch_outcome(tmp_path, d2, {"realized_pnl": -30.0})
    plain = next(d for d in read_all_decisions(tmp_path) if d["id"] == d2)
    assert realized_total(plain) == -30.0


def test_append_partial_bank_unknown_id_returns_false(tmp_path):
    assert append_partial_bank(tmp_path, "nope", {"pnl": 1.0}) is False


def test_patch_outcome_auto_stamps_r_multiple_from_total_realized(tmp_path):
    # cy78 journal-hygiene: 0/18 closes stamped r_multiple. patch_outcome now derives it from the
    # TRUE realized (incl. partial banks) / initial risk, when not explicitly supplied.
    did = append_decision(tmp_path, _decision(entry=100.0, stop=95.0, size=2.0))  # risk = 2*5 = 10
    patch_outcome(tmp_path, did, {"realized_pnl": 25.0})                          # +2.5R
    rec = next(d for d in read_all_decisions(tmp_path) if d["id"] == did)
    assert rec["r_multiple"] == 2.5
    # with a scale-out, r_multiple reflects TOTAL realized (banks + final)
    d2 = append_decision(tmp_path, _decision(cycle=2, entry=100.0, stop=95.0, size=2.0))
    append_partial_bank(tmp_path, d2, {"pnl": 5.0, "fraction": 0.5})
    patch_outcome(tmp_path, d2, {"realized_pnl": 15.0})        # total 20 / risk 10 = 2.0R
    rec2 = next(d for d in read_all_decisions(tmp_path) if d["id"] == d2)
    assert rec2["r_multiple"] == 2.0
    # an explicitly-supplied r_multiple is never overwritten
    d3 = append_decision(tmp_path, _decision(cycle=3, entry=100.0, stop=95.0, size=2.0))
    patch_outcome(tmp_path, d3, {"realized_pnl": 25.0, "r_multiple": 9.9})
    assert next(d for d in read_all_decisions(tmp_path) if d["id"] == d3)["r_multiple"] == 9.9


def test_patch_unknown_id_returns_false(tmp_path):
    append_decision(tmp_path, _decision())
    assert patch_outcome(tmp_path, "nonexistent", {"realized_pnl": 1.0}) is False


def test_decision_model_allows_extra_agent_fields():
    # Phase B agents attach extra fields; the model must tolerate them
    d = Decision(id="x", ts=datetime(2026, 5, 29, tzinfo=UTC), cycle=1,
                 symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
                 bull_thesis="...", some_future_field=123)
    dumped = d.model_dump()
    assert dumped["bull_thesis"] == "..." and dumped["some_future_field"] == 123


def test_patch_cross_month_boundary(tmp_path):
    # opened in April...
    did = append_decision(tmp_path, _decision(ts=datetime(2026, 4, 30, 23, 0, tzinfo=UTC)))
    assert (tmp_path / "episodic" / "journal-2026-04.jsonl").exists()
    # ...closed in May (different month): patch must rewrite the APRIL file, not create May
    ok = patch_outcome(tmp_path, did, {
        "exit_ts": datetime(2026, 5, 1, 2, 0, tzinfo=UTC),
        "realized_pnl": 55.0, "prediction_correct": True,
    })
    assert ok is True
    assert not (tmp_path / "episodic" / "journal-2026-05.jsonl").exists()
    rec = next(r for r in read_all_decisions(tmp_path) if r["id"] == did)
    assert rec["realized_pnl"] == 55.0
    assert rec["rationale"] == "momentum breakout"  # Phase-1 preserved across the patch
