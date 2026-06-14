"""Offline tests for the LESSONS / REFLECT CLIs (no network, injected dirs + clock).

  * retrieve_lessons_cli writes a REGIME-FILTERED ``state/cycle/<N>/lessons.json``: a lesson tagged
    for a non-matching regime is excluded while a matching one and a universal ('any') one are kept.
  * record_lessons_cli is IDEMPOTENT: re-running the same cycle's Reflector output appends each
    lesson text exactly once.
  * reflect_cli builds the winners/losers/declined contrast payload from the journal + flat log.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund.cycle_io import cycle_dir
from futures_fund.flat_journal import append_flat_decision
from futures_fund.journal import append_decision, patch_outcome
from futures_fund.lessons import append_lesson, confirm_lesson, read_lessons
from scripts import (
    promote_lesson_cli,
    record_lessons_cli,
    reflect_cli,
    retrieve_lessons_cli,
)

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


# ----------------------------------------------------------------------- retrieve (regime-filter)


def test_retrieve_writes_regime_filtered_lessons_json(tmp_path):
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    # three lessons: one for our mood, one for an unrelated mood, one universal ('any').
    append_lesson(memory, {"text": "fade the euphoric crowd", "regime": "euphoric",
                           "polarity": "restrictive"}, ts=NOW)
    append_lesson(memory, {"text": "buy capitulation washouts", "regime": "capitulation",
                           "polarity": "enabling"}, ts=NOW)
    append_lesson(memory, {"text": "never trust a lone tweet", "regime": "any",
                           "polarity": "process"}, ts=NOW)

    payload = retrieve_lessons_cli.build(
        state, memory, cycle=7, now=NOW, regime=["euphoric", "social-heavy"], tags=[], k=5,
    )

    texts = {lz["text"] for lz in payload["lessons"]}
    assert "fade the euphoric crowd" in texts          # matches the mood context
    assert "never trust a lone tweet" in texts         # universal 'any' always applies
    assert "buy capitulation washouts" not in texts    # different regime -> filtered out

    # persisted to the canonical cycle path, same contents
    written = json.loads((cycle_dir(state, 7) / "lessons.json").read_text())
    assert {lz["text"] for lz in written["lessons"]} == texts


def test_retrieve_single_regime_stays_plain_string(tmp_path):
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    append_lesson(memory, {"text": "fearful crowds overshoot down", "regime": "fearful"}, ts=NOW)
    payload = retrieve_lessons_cli.build(
        state, memory, cycle=1, now=NOW, regime="fearful", tags=[], k=5,
    )
    assert any(lz["text"] == "fearful crowds overshoot down" for lz in payload["lessons"])


# --------------------------------------------------------------------------- record (idempotent)


def test_record_lessons_is_idempotent(tmp_path):
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    cdir = cycle_dir(state, 9)
    cdir.mkdir(parents=True, exist_ok=True)
    reflector_out = {"lessons": [
        {"text": "social-only conviction needs a second source", "regime": "any",
         "importance": 7, "polarity": "restrictive"},
        {"text": "act on a credible exchange-listing rumor early", "regime": "euphoric",
         "polarity": "enabling"},
    ]}
    (cdir / "reflection_output.json").write_text(json.dumps(reflector_out))

    first = record_lessons_cli.run(state, memory, 9, NOW, input_name="reflection_output")
    assert first["appended"] == 2
    assert len(read_lessons(memory)) == 2

    # RETRY the same cycle: the same texts must be appended ZERO more times (idempotent by text).
    second = record_lessons_cli.run(state, memory, 9, NOW, input_name="reflection_output")
    assert second["appended"] == 0
    assert len(read_lessons(memory)) == 2


def test_record_lessons_tolerates_bare_list_and_key_drift(tmp_path):
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    cdir = cycle_dir(state, 3)
    cdir.mkdir(parents=True, exist_ok=True)
    # bare list + an LLM "rule" key alias instead of "text"
    (cdir / "reflection_output.json").write_text(json.dumps([
        {"rule": "cut on a thesis-invalidating headline"},
    ]))
    out = record_lessons_cli.run(state, memory, 3, NOW, input_name="reflection_output")
    assert out["appended"] == 1
    assert read_lessons(memory)[0].text == "cut on a thesis-invalidating headline"


def test_record_lessons_missing_input_is_noop(tmp_path):
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    out = record_lessons_cli.run(state, memory, 42, NOW, input_name="reflection_output")
    assert out["appended"] == 0 and out["lesson_ids"] == []


# --------------------------------------------------------------------- reflect (contrast payload)


def test_reflect_builds_winners_losers_declined_payload(tmp_path):
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    # one winner, one loser (closed); one edge-aligned FLAT the desk declined.
    win = append_decision(memory, {"id": "w", "ts": NOW, "cycle": 1, "symbol": "BTC/USDT:USDT",
                                    "direction": "long", "entry": 100.0, "stop": 98.0})
    patch_outcome(memory, win, {"realized_pnl": 50.0, "exit_ts": NOW})
    loss = append_decision(memory, {"id": "l", "ts": NOW, "cycle": 1, "symbol": "ETH/USDT:USDT",
                                     "direction": "short", "entry": 100.0, "stop": 102.0})
    patch_outcome(memory, loss, {"realized_pnl": -30.0, "exit_ts": NOW})
    append_flat_decision(memory, {"cycle": 1, "symbol": "SOL/USDT:USDT", "edge_aligned": True,
                                  "favored_side": "long", "mark": 10.0}, ts=NOW)

    payload = reflect_cli.build(state, memory, cycle=5)

    assert payload["n_closed"] == 2
    assert [d["id"] for d in payload["winners"]] == ["w"]
    assert [d["id"] for d in payload["losers"]] == ["l"]
    assert len(payload["declined_edge_setups"]) == 1
    # persisted to the canonical cycle path
    written = json.loads((cycle_dir(state, 5) / "reflection_input.json").read_text())
    assert written["n_closed"] == 2


# --------------------------------------------------------------------------- promote (stats-gated)


def _winning_book(memory):
    """Five closed winners with positive R-multiples -> strong statistical support (p ~ 1)."""
    for i in range(5):
        did = append_decision(memory, {"id": f"d{i}", "ts": NOW, "cycle": i + 1,
                                       "symbol": "BTC/USDT:USDT", "direction": "long",
                                       "entry": 100.0, "stop": 98.0, "size": 1.0})
        patch_outcome(memory, did, {"realized_pnl": 4.0 + 0.1 * i, "exit_ts": NOW})


def test_promote_confirm_promotes_with_statistical_support(tmp_path):
    memory = tmp_path / "memory"
    lid = append_lesson(memory, {"text": "ride confirmed squeeze-longs", "regime": "any"}, ts=NOW)
    # accrue 4 prior confirmations so the 5th (via the CLI) crosses the count threshold.
    for _ in range(4):
        confirm_lesson(memory, lid, promote_threshold=5)

    _winning_book(memory)
    out = promote_lesson_cli.run(memory, lid, "confirm")

    assert out["ok"] is True
    assert out["dsr_pvalue"] >= 0.95  # winning book -> mean R clearly > 0
    promoted = next(lz for lz in read_lessons(memory) if lz.id == lid)
    assert promoted.state == "validated"


def test_promote_confirm_blocks_without_support(tmp_path):
    memory = tmp_path / "memory"
    lid = append_lesson(memory, {"text": "unproven hunch", "regime": "any"}, ts=NOW)
    for _ in range(4):
        confirm_lesson(memory, lid, promote_threshold=5)

    # no closed trades -> statistical_support == 0.0 -> count crosses but the stats gate HOLDS.
    out = promote_lesson_cli.run(memory, lid, "confirm")
    assert out["ok"] is True and out["dsr_pvalue"] == 0.0
    held = next(lz for lz in read_lessons(memory) if lz.id == lid)
    assert held.state == "candidate" and held.confirmations == 5


def test_promote_demote_and_retire(tmp_path):
    memory = tmp_path / "memory"
    lid = append_lesson(memory, {"text": "stale rule", "regime": "any"}, ts=NOW)
    assert promote_lesson_cli.run(memory, lid, "retire")["ok"] is True
    assert next(lz for lz in read_lessons(memory) if lz.id == lid).state == "retired"
    assert promote_lesson_cli.run(memory, "missing-id", "demote")["ok"] is False
