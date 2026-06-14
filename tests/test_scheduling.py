"""Tests for the hourly-poll + 4h-candle-boundary due-gate (futures_fund.scheduling.cycle_due).

Derived directly from the design red-team's vetted test_matrix. The predicate gates on the
SERVED CANDLE (report['candle'] = floor4(gate-start)) of the highest cycle with a PARSEABLE
report.json — never on completion time, never on max(dir). All datetimes are tz-aware UTC.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from futures_fund.scheduling import crawl_due, cycle_due, floor4, floor_n, stamp_crawl

UTC = timezone.utc


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _write_report(state_dir, n: int, *, candle: str | None = None, ran_at: str | None = None,
                  mtime: str | None = None, raw: str | None = None, halted: bool = False,
                  actions: list | None = None) -> None:
    """Create state/cycle/<n>/report.json. `raw` overrides with literal bytes (for corrupt JSON).
    `mtime` (ISO UTC) sets the file mtime via os.utime."""
    d = state_dir / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    p = d / "report.json"
    if raw is not None:
        p.write_text(raw)
    else:
        rep: dict = {"cycle": n, "halted": halted, "actions": actions or []}
        if candle is not None:
            rep["candle"] = candle
        if ran_at is not None:
            rep["ran_at"] = ran_at
        p.write_text(json.dumps(rep))
    if mtime is not None:
        ts = _dt(mtime).timestamp()
        os.utime(p, (ts, ts))


def _bare_dir(state_dir, n: int, *, with_universe: bool = False) -> None:
    """A cycle dir that crashed before the gate: exists, no report.json."""
    d = state_dir / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    if with_universe:
        (d / "universe.json").write_text("{}")


def _is_due(result) -> bool:
    return result[0] in ("FRESH", "RETRY")


# --------------------------------------------------------------------------- floor4

def test_floor4_grid():
    assert floor4(_dt("2026-05-31T12:07:00+00:00")) == _dt("2026-05-31T12:00:00+00:00")
    assert floor4(_dt("2026-05-31T15:59:59+00:00")) == _dt("2026-05-31T12:00:00+00:00")
    assert floor4(_dt("2026-05-31T23:30:00+00:00")) == _dt("2026-05-31T20:00:00+00:00")
    assert floor4(_dt("2026-05-31T00:00:00+00:00")) == _dt("2026-05-31T00:00:00+00:00")
    assert floor4(_dt("2026-05-31T03:59:00+00:00")) == _dt("2026-05-31T00:00:00+00:00")


def test_floor4_rejects_naive():
    with pytest.raises(AssertionError):
        floor4(datetime(2026, 5, 31, 12, 0, 0))


# --------------------------------------------------------------------------- matrix

def test_cold_start_no_cycle_dir(tmp_path):
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T11:07:00+00:00")))


def test_cold_start_first_run_same_window_no_double_fire(tmp_path):
    _write_report(tmp_path, 1, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T11:40:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_cold_start_same_candle_repoll_skips(tmp_path):
    _write_report(tmp_path, 1, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T11:40:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T11:50:00+00:00"))[0] == "SKIP"


def test_normal_within_candle_skip(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T09:07:00+00:00"))[0] == "SKIP"


def test_normal_within_candle_skip_last_poll(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T11:07:00+00:00"))[0] == "SKIP"


def test_new_candle_due_is_fresh_next_n(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode == "FRESH" and n == 8


def test_missed_boundary_catchup_within_1h(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T13:07:00+00:00"))
    assert mode == "FRESH" and n == 8


def test_multi_boundary_outage_single_catchup(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T04:00:00+00:00", ran_at="2026-05-31T04:30:00+00:00")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T15:37:00+00:00"))
    assert mode == "FRESH" and n == 8


def test_multi_boundary_outage_next_poll_new_boundary_due(tmp_path):
    _write_report(tmp_path, 8, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T15:50:00+00:00")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T16:00:00+00:00"))
    assert mode == "FRESH" and n == 9


def test_late_finish_boundary_crossing_does_not_steal_next_candle(tmp_path):
    # candle = floor of START (12:00) even though ran_at completion crossed into 16:00
    _write_report(tmp_path, 8, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T16:02:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T16:07:00+00:00")))


def test_boundary_exact_instant_unserved_due(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:00:00+00:00")))


def test_boundary_exact_instant_already_served_skips(tmp_path):
    # served_candle == boundary, and ran_at slightly ahead of now (future-ran_at guard must not flip it)
    _write_report(tmp_path, 7, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T12:00:05+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T12:00:00+00:00"))[0] == "SKIP"


def test_crashed_midcycle_missing_report_retries_that_dir(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    _bare_dir(tmp_path, 8, with_universe=True)  # crashed before gate
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode == "RETRY" and n == 8


def test_phantom_high_numbered_empty_dir_does_not_stall(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    _bare_dir(tmp_path, 99)
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode == "RETRY" and n == 99


def test_phantom_dir_same_candle_already_served_still_skips(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T12:25:00+00:00")
    _bare_dir(tmp_path, 99)
    assert cycle_due(tmp_path, _dt("2026-05-31T12:40:00+00:00"))[0] == "SKIP"


def test_unparseable_report_falls_to_prior_completed_retry(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    _write_report(tmp_path, 8, raw="{ this is : not valid json ")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode == "RETRY" and n == 8


def test_unparseable_report_same_candle_prior_completed_skips(tmp_path):
    # NOTE: matrix row's `expected` field said DUE but its name + the documented predicate say SKIP.
    # cycle 7 already served the 12:00 candle, so a corrupt higher dir must NOT re-run it. SKIP is correct.
    _write_report(tmp_path, 7, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T12:25:00+00:00")
    _write_report(tmp_path, 8, raw="{bad json")
    assert cycle_due(tmp_path, _dt("2026-05-31T12:50:00+00:00"))[0] == "SKIP"


def test_absent_ran_at_and_candle_mtime_fallback_due(tmp_path):
    _write_report(tmp_path, 7, mtime="2026-05-31T08:27:00+00:00")  # legacy: no candle/ran_at
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_absent_ran_at_mtime_fallback_same_candle_skip(tmp_path):
    _write_report(tmp_path, 7, mtime="2026-05-31T12:25:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T12:50:00+00:00"))[0] == "SKIP"


def test_malformed_ran_at_empty_string_falls_to_mtime(tmp_path):
    _write_report(tmp_path, 7, ran_at="", mtime="2026-05-31T08:27:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_naive_ran_at_no_offset_coerced_utc_no_typeerror(tmp_path):
    _write_report(tmp_path, 7, ran_at="2026-05-31T08:27:00")  # naive, no offset
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_z_suffix_ran_at_parses(tmp_path):
    _write_report(tmp_path, 7, ran_at="2026-05-31T08:27:00Z")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_tz_aware_host_in_cest_no_skew(tmp_path):
    """mtime fallback must use fromtimestamp(..., tz=UTC) so a CEST host does not skew the candle."""
    if not hasattr(time, "tzset"):
        pytest.skip("tzset unavailable")
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Zurich"
        time.tzset()
        _write_report(tmp_path, 7, mtime="2026-05-31T08:27:00+00:00")  # 10:27 local CEST
        assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_clock_moved_backward_bounded_skip(tmp_path):
    # Real time ~12:25 (12:00 candle served) but host clock jumped back to 11:07.
    # served_candle 12:00 is only one step ahead of boundary 08:00 -> trusted -> bounded SKIP.
    _write_report(tmp_path, 7, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T12:25:00+00:00")
    assert cycle_due(tmp_path, _dt("2026-05-31T11:07:00+00:00"))[0] == "SKIP"


def test_future_ran_at_skew_does_not_wedge(tmp_path):
    _write_report(tmp_path, 7, ran_at="2026-05-31T20:00:00+00:00", mtime="2026-05-31T08:27:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_future_candle_field_distrusted_uses_prior_cycle(tmp_path):
    # cycle 8 has an egregiously-future candle (>1 step ahead) -> distrust, fall to cycle 7.
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    _write_report(tmp_path, 8, candle="2026-06-01T00:00:00+00:00", ran_at="2026-06-01T00:05:00+00:00")
    assert _is_due(cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00")))


def test_stand_down_cycle_marks_candle_no_double_fire(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T12:20:00+00:00",
                  actions=[])
    assert cycle_due(tmp_path, _dt("2026-05-31T13:07:00+00:00"))[0] == "SKIP"


def test_halt_cycle_marks_candle_no_hourly_thrash(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T12:00:00+00:00", ran_at="2026-05-31T12:15:00+00:00",
                  halted=True)
    assert cycle_due(tmp_path, _dt("2026-05-31T14:07:00+00:00"))[0] == "SKIP"


# --------------------------------------------------------------- fail-safe & determinism

def test_never_raises_on_garbage_state(tmp_path):
    (tmp_path / "cycle").mkdir()
    (tmp_path / "cycle" / "notanumber").mkdir()        # non-numeric dir ignored
    _write_report(tmp_path, 5, raw="\x00\x01 garbage")  # binary garbage
    # must not raise, must yield a decision
    mode, n, reason = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode in ("FRESH", "RETRY", "SKIP")


def test_non_numeric_dirs_excluded_from_n(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:27:00+00:00")
    (tmp_path / "cycle" / "scratch").mkdir()
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode == "FRESH" and n == 8


# --- verify-pass regression: non-dict report.json must behave like a crashed dir, not crash ---

def test_non_dict_report_json_null_retries_not_fresh1(tmp_path):
    # `null` is valid JSON but not a dict; must NOT escape as AttributeError -> fail-safe FRESH 1.
    _write_report(tmp_path, 7, raw="null")
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert (mode, n) == ("RETRY", 7)


def test_non_dict_report_json_list_scans_to_prior_completed(tmp_path):
    _write_report(tmp_path, 7, candle="2026-05-31T04:00:00+00:00", ran_at="2026-05-31T04:20:00+00:00")
    _write_report(tmp_path, 8, candle="2026-05-31T08:00:00+00:00", ran_at="2026-05-31T08:20:00+00:00")
    _write_report(tmp_path, 9, raw='[{"cycle": 9}]')  # JSON list, not dict
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert (mode, n) == ("RETRY", 9)


def test_non_dict_report_json_scalar_does_not_crash(tmp_path):
    _write_report(tmp_path, 7, raw="42")  # bare scalar
    mode, n, _ = cycle_due(tmp_path, _dt("2026-05-31T12:07:00+00:00"))
    assert mode in ("FRESH", "RETRY", "SKIP")  # must not fail-safe to FRESH 1 via AttributeError
    assert n == 7  # RETRY the lone uncompleted dir, never overwrite ancient cycle 1


# --- verify-pass regression: a foreign tz offset in ran_at must normalize to UTC ---

def test_foreign_offset_ran_at_normalized_to_utc(tmp_path):
    # 13:57+05:30 == 08:27 UTC -> served candle 08:00; at 09:07 (boundary 08:00) that is SKIP.
    _write_report(tmp_path, 7, ran_at="2026-05-31T13:57:00+05:30")  # no candle field
    assert cycle_due(tmp_path, _dt("2026-05-31T09:07:00+00:00"))[0] == "SKIP"


# ====================================================================== floor_n
# The N-minute grid primitive for the 15-minute crawler loop.

def test_floor_n_15min_grid():
    assert floor_n(_dt("2026-06-13T12:07:00+00:00"), 15) == _dt("2026-06-13T12:00:00+00:00")
    assert floor_n(_dt("2026-06-13T12:14:59+00:00"), 15) == _dt("2026-06-13T12:00:00+00:00")
    assert floor_n(_dt("2026-06-13T12:15:00+00:00"), 15) == _dt("2026-06-13T12:15:00+00:00")
    assert floor_n(_dt("2026-06-13T12:44:30+00:00"), 15) == _dt("2026-06-13T12:30:00+00:00")
    assert floor_n(_dt("2026-06-13T12:45:00+00:00"), 15) == _dt("2026-06-13T12:45:00+00:00")
    assert floor_n(_dt("2026-06-13T12:59:59+00:00"), 15) == _dt("2026-06-13T12:45:00+00:00")


def test_floor_n_strips_seconds_micros():
    out = floor_n(datetime(2026, 6, 13, 12, 31, 42, 500000, tzinfo=UTC), 15)
    assert out == _dt("2026-06-13T12:30:00+00:00")
    assert out.second == 0 and out.microsecond == 0


def test_floor_n_other_intervals():
    assert floor_n(_dt("2026-06-13T12:07:00+00:00"), 5) == _dt("2026-06-13T12:05:00+00:00")
    assert floor_n(_dt("2026-06-13T12:31:00+00:00"), 30) == _dt("2026-06-13T12:30:00+00:00")
    assert floor_n(_dt("2026-06-13T12:31:00+00:00"), 60) == _dt("2026-06-13T12:00:00+00:00")


def test_floor_n_rejects_naive():
    with pytest.raises(AssertionError):
        floor_n(datetime(2026, 6, 13, 12, 0, 0), 15)


def test_floor_n_garbage_interval_clamps_to_1():
    # non-positive / garbage interval must not raise (degenerate 1-minute grid)
    assert floor_n(_dt("2026-06-13T12:07:30+00:00"), 0) == _dt("2026-06-13T12:07:00+00:00")
    assert floor_n(_dt("2026-06-13T12:07:30+00:00"), -5) == _dt("2026-06-13T12:07:00+00:00")


# ===================================================================== crawl_due
# 15-min grid gate on state/crawl/last_crawl.json. mode in {"DUE","SKIP"}.

def test_crawl_due_cold_start_no_state_is_due(tmp_path):
    mode, _ = crawl_due(tmp_path, _dt("2026-06-13T12:07:00+00:00"))
    assert mode == "DUE"


def test_crawl_due_same_slot_skips(tmp_path):
    stamp_crawl(tmp_path, _dt("2026-06-13T12:02:00+00:00"))   # serves the 12:00 slot
    assert crawl_due(tmp_path, _dt("2026-06-13T12:14:00+00:00"))[0] == "SKIP"


def test_crawl_due_new_slot_is_due(tmp_path):
    stamp_crawl(tmp_path, _dt("2026-06-13T12:02:00+00:00"))   # serves 12:00 slot
    assert crawl_due(tmp_path, _dt("2026-06-13T12:16:00+00:00"))[0] == "DUE"   # 12:15 slot


def test_crawl_due_exact_slot_boundary_is_due(tmp_path):
    stamp_crawl(tmp_path, _dt("2026-06-13T12:02:00+00:00"))   # serves 12:00 slot
    # 12:15:00 is the START of a new slot, strictly later than 12:00 -> DUE
    assert crawl_due(tmp_path, _dt("2026-06-13T12:15:00+00:00"))[0] == "DUE"


def test_crawl_due_multi_slot_outage_single_catchup(tmp_path):
    # last served 12:00; now 13:07 (the 13:00 slot). A single catch-up serves the CURRENT slot,
    # it does not replay every missed 15-min slot in between.
    stamp_crawl(tmp_path, _dt("2026-06-13T12:00:00+00:00"))
    assert crawl_due(tmp_path, _dt("2026-06-13T13:07:00+00:00"))[0] == "DUE"
    # and a second poll inside that same 13:00 slot then SKIPs once stamped
    stamp_crawl(tmp_path, _dt("2026-06-13T13:07:00+00:00"))
    assert crawl_due(tmp_path, _dt("2026-06-13T13:12:00+00:00"))[0] == "SKIP"


def test_crawl_due_missing_state_file_unreadable_fail_safe_due(tmp_path):
    p = tmp_path / "crawl" / "last_crawl.json"
    p.parent.mkdir(parents=True)
    p.write_text("{ not valid json")
    assert crawl_due(tmp_path, _dt("2026-06-13T12:07:00+00:00"))[0] == "DUE"


def test_crawl_due_state_missing_last_slot_fail_safe_due(tmp_path):
    p = tmp_path / "crawl" / "last_crawl.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"stamped_at": "2026-06-13T12:00:00+00:00"}))  # no last_slot
    assert crawl_due(tmp_path, _dt("2026-06-13T12:07:00+00:00"))[0] == "DUE"


def test_crawl_due_non_object_state_fail_safe_due(tmp_path):
    p = tmp_path / "crawl" / "last_crawl.json"
    p.parent.mkdir(parents=True)
    p.write_text("[1, 2, 3]")  # valid JSON, not an object
    assert crawl_due(tmp_path, _dt("2026-06-13T12:07:00+00:00"))[0] == "DUE"


def test_crawl_due_off_grid_stamp_normalized(tmp_path):
    # a hand-written off-grid last_slot (:07) floors to the :00 slot it actually served, so a poll
    # in the SAME :00 slot SKIPs and the next :15 slot is DUE.
    p = tmp_path / "crawl" / "last_crawl.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"last_slot": "2026-06-13T12:07:00+00:00"}))
    assert crawl_due(tmp_path, _dt("2026-06-13T12:09:00+00:00"))[0] == "SKIP"
    assert crawl_due(tmp_path, _dt("2026-06-13T12:16:00+00:00"))[0] == "DUE"


def test_crawl_due_custom_interval(tmp_path):
    stamp_crawl(tmp_path, _dt("2026-06-13T12:02:00+00:00"), interval_min=5)  # serves 12:00 slot
    assert crawl_due(tmp_path, _dt("2026-06-13T12:04:00+00:00"), interval_min=5)[0] == "SKIP"
    assert crawl_due(tmp_path, _dt("2026-06-13T12:06:00+00:00"), interval_min=5)[0] == "DUE"


def test_stamp_crawl_writes_floored_slot_atomically(tmp_path):
    p = stamp_crawl(tmp_path, _dt("2026-06-13T12:13:00+00:00"))
    assert p == tmp_path / "crawl" / "last_crawl.json"
    payload = json.loads(p.read_text())
    assert payload["last_slot"] == "2026-06-13T12:00:00+00:00"  # floored to the 15-min grid
    assert payload["interval_min"] == 15
    # no leftover temp file
    assert not (tmp_path / "crawl" / "last_crawl.json.tmp").exists()
