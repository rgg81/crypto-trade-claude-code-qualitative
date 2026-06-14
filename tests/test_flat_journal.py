"""FLAT-decision journal: persist declined setups + later evaluate whether standing aside cost us.
This is the data source that lets the Reflector mint ENABLING ('DO take it') lessons, not only
'don't' lessons (the one-directional ratchet)."""
from datetime import UTC, datetime

from futures_fund.flat_journal import (
    append_flat_decision,
    coerce_flat_verdicts,
    evaluate_pending_flats,
    read_flat_decisions,
)


def test_coerce_flat_verdicts_accepts_bare_list():
    rows = [{"symbol": "XRPUSDT", "rating": "flat"}]
    assert coerce_flat_verdicts(rows) == rows


def test_coerce_flat_verdicts_unwraps_wrapper_dict():
    rows = [{"symbol": "XRPUSDT", "rating": "flat"}]
    # the orchestrator mis-wrapping the file as {"flat_verdicts": [...]} must not crash the stage
    assert coerce_flat_verdicts({"flat_verdicts": rows}) == rows
    assert coerce_flat_verdicts({"verdicts": rows}) == rows


def test_coerce_flat_verdicts_empty_and_garbage_are_safe():
    assert coerce_flat_verdicts({}) == []
    assert coerce_flat_verdicts(None) == []
    # a wrapper whose payload is not a list yields empty, never a str-iteration crash
    assert coerce_flat_verdicts({"flat_verdicts": "oops"}) == []


def _flat(symbol, *, edge=True, side="long", mark=100.0, cycle=9):
    return {"cycle": cycle, "symbol": symbol, "regime": "high_vol_trend", "rating": "flat",
            "reason": "no fillable entry", "edge_aligned": edge, "favored_side": side, "mark": mark}


def test_append_and_read_roundtrip(tmp_path):
    fid = append_flat_decision(tmp_path, _flat("XLMUSDT"), ts=datetime(2026, 5, 31, tzinfo=UTC))
    rows = read_flat_decisions(tmp_path)
    assert len(rows) == 1 and rows[0]["id"] == fid
    assert rows[0]["edge_aligned"] is True and rows[0]["evaluated"] is False


def test_evaluate_marks_flat_that_cost_us(tmp_path):
    # passed on a LONG at 100; price later 105 (+5% our way) -> standing aside COST us
    append_flat_decision(tmp_path, _flat("XLMUSDT", side="long", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    n = evaluate_pending_flats(tmp_path, {"XLMUSDT": 105.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    assert n == 1
    r = read_flat_decisions(tmp_path)[0]
    assert r["evaluated"] is True and r["flat_cost_us"] is True
    assert abs(r["favored_move_pct"] - 0.05) < 1e-9


def test_evaluate_marks_flat_that_was_right(tmp_path):
    # passed on a LONG at 100; price later 96 (-4% against the long) -> standing aside was RIGHT
    append_flat_decision(tmp_path, _flat("ZECUSDT", side="long", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"ZECUSDT": 96.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    r = read_flat_decisions(tmp_path)[0]
    assert r["flat_cost_us"] is False


def test_short_side_favored_move_sign(tmp_path):
    # passed on a SHORT at 100; price later 90 (-10%) -> favored move is +10% (cost us)
    append_flat_decision(tmp_path, _flat("SUIUSDT", side="short", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"SUIUSDT": 90.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    r = read_flat_decisions(tmp_path)[0]
    assert abs(r["favored_move_pct"] - 0.10) < 1e-9 and r["flat_cost_us"] is True


def test_non_edge_aligned_flats_are_not_evaluated(tmp_path):
    append_flat_decision(tmp_path, _flat("DOGEUSDT", edge=False),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    n = evaluate_pending_flats(tmp_path, {"DOGEUSDT": 200.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    assert n == 0 and read_flat_decisions(tmp_path)[0]["evaluated"] is False


def test_evaluate_is_idempotent(tmp_path):
    append_flat_decision(tmp_path, _flat("XLMUSDT", mark=100.0),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"XLMUSDT": 105.0}, datetime(2026, 5, 31, 4, tzinfo=UTC))
    again = evaluate_pending_flats(tmp_path, {"XLMUSDT": 110.0},
                                   datetime(2026, 5, 31, 8, tzinfo=UTC))
    assert again == 0  # already evaluated, not re-scored


# --- multi-day eval horizon + max favorable excursion (fix: stop scoring the 1-cycle bounce) ----
def test_horizon_gating_defers_then_finalizes(tmp_path):
    # a flat decided at cycle 9 is NOT finalized 1 cycle later; only after the multi-day horizon
    append_flat_decision(tmp_path, _flat("XLMUSDT", side="short", mark=100.0, cycle=9),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    n1 = evaluate_pending_flats(tmp_path, {"XLMUSDT": 98.0}, datetime(2026, 5, 31, 4, tzinfo=UTC),
                                now_cycle=10, eval_after_cycles=6)
    assert n1 == 0 and read_flat_decisions(tmp_path)[0]["evaluated"] is False  # deferred
    n2 = evaluate_pending_flats(tmp_path, {"XLMUSDT": 90.0}, datetime(2026, 6, 1, 4, tzinfo=UTC),
                                now_cycle=15, eval_after_cycles=6)
    assert n2 == 1 and read_flat_decisions(tmp_path)[0]["evaluated"] is True


def test_uses_max_favorable_excursion_not_endpoint(tmp_path):
    # declined SHORT at 100: trends our way to 80 (+20%) mid-window, then round-trips to 98 (+2%) at
    # the horizon. Verdict must reflect the +20% the trade would have captured (TPs), not the +2%.
    append_flat_decision(tmp_path, _flat("ETHUSDT", side="short", mark=100.0, cycle=9),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"ETHUSDT": 80.0}, datetime(2026, 5, 31, 8, tzinfo=UTC),
                           now_cycle=11, eval_after_cycles=6)   # mid-window peak, still pending
    assert read_flat_decisions(tmp_path)[0]["evaluated"] is False
    evaluate_pending_flats(tmp_path, {"ETHUSDT": 98.0}, datetime(2026, 6, 1, 4, tzinfo=UTC),
                           now_cycle=15, eval_after_cycles=6)   # round-trip endpoint, finalize
    r = read_flat_decisions(tmp_path)[0]
    assert r["evaluated"] is True and r["flat_cost_us"] is True
    assert abs(r["favored_move_pct"] - 0.20) < 1e-9    # MAX excursion, not the 0.02 endpoint
    assert abs(r["endpoint_move_pct"] - 0.02) < 1e-9   # endpoint recorded for transparency


def test_short_horizon_bounce_no_longer_locks_verdict(tmp_path):
    # the bug: a declined SHORT that bounces +2% (against) next candle USED to lock cost_us=False.
    # now the bounce is deferred (not finalized), and the later trend decides the verdict.
    append_flat_decision(tmp_path, _flat("ADAUSDT", side="short", mark=100.0, cycle=9),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    evaluate_pending_flats(tmp_path, {"ADAUSDT": 102.0}, datetime(2026, 5, 31, 4, tzinfo=UTC),
                           now_cycle=10, eval_after_cycles=6)   # +2% against -> NOT locked
    assert read_flat_decisions(tmp_path)[0]["evaluated"] is False
    evaluate_pending_flats(tmp_path, {"ADAUSDT": 85.0}, datetime(2026, 6, 1, 4, tzinfo=UTC),
                           now_cycle=15, eval_after_cycles=6)   # trend down 15% -> cost us
    r = read_flat_decisions(tmp_path)[0]
    assert r["evaluated"] is True and r["flat_cost_us"] is True


def test_overdue_unrepriceable_flat_finalizes_instead_of_replaying(tmp_path):
    # cy78 P1c: a pending flat whose symbol DROPPED from the universe (absent from marks) can never
    # re-price or finalize -> it replayed its stale snapshot forever (the cy37 ZEC bug, 41 cycles).
    # Once well past the horizon it must FINALIZE (on the last running peak), not replay.
    append_flat_decision(tmp_path, _flat("ZECUSDT", side="long", mark=316.30, cycle=37),
                         ts=datetime(2026, 5, 31, tzinfo=UTC))
    # cycle 80, ZEC NOT in this cycle's marks -> overdue + un-repriceable -> finalize
    n = evaluate_pending_flats(tmp_path, {"BTCUSDT": 64000.0}, datetime(2026, 6, 1, tzinfo=UTC),
                               now_cycle=80, eval_after_cycles=6)
    assert n == 1
    row = read_flat_decisions(tmp_path)[0]
    assert row["evaluated"] is True and row.get("stale_unrepriced") is True
    # a never-moved stale flat did NOT cost us -> must not wrongly indict the conservatism
    assert row["flat_cost_us"] is False


def test_recent_unrepriceable_flat_stays_pending(tmp_path):
    # not yet overdue + un-repriceable -> still pending (don't finalize prematurely)
    append_flat_decision(tmp_path, _flat("ZECUSDT", cycle=79), ts=datetime(2026, 5, 31, tzinfo=UTC))
    n = evaluate_pending_flats(tmp_path, {"BTCUSDT": 64000.0}, datetime(2026, 6, 1, tzinfo=UTC),
                               now_cycle=80, eval_after_cycles=6)
    assert n == 0 and read_flat_decisions(tmp_path)[0]["evaluated"] is False
