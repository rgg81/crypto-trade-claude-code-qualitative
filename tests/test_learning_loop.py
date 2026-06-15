"""End-to-end OFFLINE verification of the trade-outcome LEARNING LOOP (no network, tmp dirs only).

The qualitative desk's self-improvement promise is that it gets a little sharper every four hours:
a closed trade -> a Reflector contrast -> a recorded CANDIDATE lesson -> (statistical) promotion ->
retrieval into the NEXT cycle's debate. This test exercises the SAME library functions the four CLIs
call (it does NOT re-implement them, and it does NOT patch the protected reflect/lessons logic), so we
can confirm the loop closes at the LIBRARY level before it is wired into the 4h DECISION cycle:

    reflect_cli.build            -> futures_fund.reflect.reflection_payload
    record_lessons_cli.run       -> futures_fund.reflect.record_lessons -> .record_lesson
                                                                         -> lessons.append_lesson
    promote_lesson_cli.run       -> futures_fund.lessons.statistically_promote (+ journal R-multiples)
    retrieve_lessons_cli.build   -> futures_fund.lessons.retrieve_lessons

Each test injects a tmp `memory`/`state` dir and a frozen clock; nothing touches the network or the
real corpus.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from futures_fund.cycle_io import cycle_dir
from futures_fund.journal import append_decision, patch_outcome, read_all_decisions, realized_total
from futures_fund.lessons import read_lessons
from scripts import (
    promote_lesson_cli,
    record_lessons_cli,
    reflect_cli,
    retrieve_lessons_cli,
)

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)

# The crowd-mood quadrant the closed trade was taken in; the Reflector tags its lesson with this and
# the NEXT cycle retrieves it back by the same regime context -> the loop closes.
REGIME = "capitulation"
TAGS = ["capitulation", "flow", "contrarian", "long"]

# A reflector-style output in the EXACT shape agents/reflector.md specifies: {"lessons": [...]}, an
# ENABLING lesson (mined from a winner that faded the despair) provenance-cited to the closed trade.
LESSON_TEXT = (
    "When two of three experts read forced-seller capitulation (despair tone, faded attention), "
    "fading it long worked. DO take the exhausted-seller long at a crowd-capitulation extreme."
)


def _closed_winner(memory, *, did: str, cycle: int, ts: datetime, pnl: float = 50.0):
    """Craft a CLOSED-trade journal entry: a Phase-1 Decision opened then patched with a Phase-2
    realized outcome (the ground-truth signal the whole loop learns from)."""
    rid = append_decision(memory, {
        "id": did, "ts": ts, "cycle": cycle, "symbol": "BTC/USDT:USDT", "direction": "long",
        "entry": 100.0, "stop": 98.0, "size": 1.0, "regime": REGIME,
        "falsifiable_prediction": "despair reverts within 24h", "setup": "fade-capitulation",
    })
    patch_outcome(memory, rid, {"realized_pnl": pnl, "exit_ts": ts, "prediction_correct": True})
    return rid


# ----------------------------------------------------------------- the full loop closes end-to-end


def test_learning_loop_closes_reflect_record_retrieve(tmp_path):
    """reflect -> record -> retrieve: a closed winner becomes a lesson that the NEXT cycle gets back.

    This is the load-bearing integration: it proves the self-improvement subsystem can be safely
    wired into the cycle, because the recorded lesson is retrievable for the SAME regime it was
    minted in."""
    memory = tmp_path / "memory"
    state = tmp_path / "state"

    # 1. A closed trade exists in the journal (realized_pnl + outcome patched on close).
    win_id = _closed_winner(memory, did="dec-2026-06-13-BTC", cycle=10, ts=NOW)

    # 1b. The reflect phase builds the winners/losers/declined contrast the Reflector reads, and
    #     persists it to state/cycle/<N>/reflection_input.json (what scripts/reflect_cli.py writes).
    payload = reflect_cli.build(state, memory, cycle=11)
    assert payload["n_closed"] == 1
    assert [d["id"] for d in payload["winners"]] == [win_id]  # the closed winner is the source row
    assert payload["losers"] == []
    written_input = json.loads((cycle_dir(state, 11) / "reflection_input.json").read_text())
    assert written_input["n_closed"] == 1

    # 2. Feed a reflector-style output ({"lessons":[...]}) into record_lessons (via the CLI's run()).
    #    The Reflector subagent would produce this from `reflection_input`; we persist it where the
    #    CLI reads it, then let record_lessons_cli.run -> reflect.record_lessons append it.
    reflector_out = {"lessons": [{
        "text": LESSON_TEXT, "polarity": "enabling", "regime": REGIME, "tags": TAGS,
        "importance": 7, "provenance": [win_id],
    }]}
    (cycle_dir(state, 11) / "reflection_output.json").write_text(json.dumps(reflector_out))

    first = record_lessons_cli.run(state, memory, 11, NOW, input_name="reflection_output")
    assert first["appended"] == 1
    corpus = read_lessons(memory)
    assert len(corpus) == 1
    minted = corpus[0]
    assert minted.text == LESSON_TEXT
    assert minted.polarity == "enabling"          # the corpus stays two-sided (not only "don't")
    assert minted.regime == REGIME
    assert minted.state == "candidate"            # fresh lessons are CANDIDATE; promotion is gated
    assert minted.provenance == [win_id]          # cited to the source decision (no anonymous wisdom)

    # 2b. IDEMPOTENT on re-run (a DUE RETRY of the same cycle): the same text is appended ZERO times.
    second = record_lessons_cli.run(state, memory, 11, NOW, input_name="reflection_output")
    assert second["appended"] == 0
    assert len(read_lessons(memory)) == 1

    # 3. The NEXT cycle retrieves lessons for the matching regime/tags -> the minted lesson returns,
    #    persisted to state/cycle/<N>/lessons.json for injection into the debate/decider prompts.
    next_now = NOW + timedelta(hours=4)           # one 4h cycle later
    retrieved = retrieve_lessons_cli.build(
        state, memory, cycle=12, now=next_now,
        regime=[REGIME, "social-heavy"], tags=TAGS, k=5,
    )
    texts = {lz["text"] for lz in retrieved["lessons"]}
    assert LESSON_TEXT in texts                    # LOOP CLOSED: reflect -> record -> retrieve
    written_lessons = json.loads((cycle_dir(state, 12) / "lessons.json").read_text())
    assert {lz["text"] for lz in written_lessons["lessons"]} == texts


def test_recorded_lesson_filtered_out_for_a_different_regime(tmp_path):
    """A retrieved lesson is regime-RELEVANT: the capitulation lesson does NOT surface in an
    unrelated (euphoric) crowd mood — so the loop injects the RIGHT lesson, not noise."""
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    _closed_winner(memory, did="dec-1", cycle=1, ts=NOW)
    (cycle_dir(state, 2)).mkdir(parents=True, exist_ok=True)
    (cycle_dir(state, 2) / "reflection_output.json").write_text(json.dumps(
        {"lessons": [{"text": LESSON_TEXT, "polarity": "enabling", "regime": REGIME,
                      "tags": TAGS, "importance": 7}]}))
    record_lessons_cli.run(state, memory, 2, NOW, input_name="reflection_output")

    # query a different mood quadrant -> the capitulation-tagged lesson is filtered out.
    other = retrieve_lessons_cli.build(state, memory, cycle=3, now=NOW,
                                       regime=["euphoric", "social-heavy"], tags=TAGS, k=5)
    assert LESSON_TEXT not in {lz["text"] for lz in other["lessons"]}


# --------------------------------------------------- promotion arm of the loop (reflect->...->promote)


def _winning_book(memory, ts: datetime, n: int = 5):
    """`n` closed winners with positive R-multiples -> strong statistical support (DSR p ~ 1).

    Each open has clean geometry (size * |entry-stop|) so journal auto-stamps r_multiple at close,
    which promote_lesson_cli re-derives the desk's edge p-value from."""
    for i in range(n):
        did = append_decision(memory, {
            "id": f"book-{i}", "ts": ts, "cycle": 100 + i, "symbol": "BTC/USDT:USDT",
            "direction": "long", "entry": 100.0, "stop": 98.0, "size": 1.0, "regime": REGIME,
        })
        patch_outcome(memory, did, {"realized_pnl": 4.0 + 0.1 * i, "exit_ts": ts})


def test_loop_promotes_candidate_to_validated_with_statistical_support(tmp_path):
    """reflect -> record -> CONFIRM -> retrieve, full arm: a recurring CANDIDATE lesson promotes to
    VALIDATED only when the closed-trade record statistically supports the edge, and a VALIDATED
    standing rule is always retrieved back (never dropped by the polarity quota)."""
    memory = tmp_path / "memory"
    state = tmp_path / "state"

    # Build a winning closed-trade book so the desk's edge p-value clears the stats gate.
    _winning_book(memory, NOW)
    # sanity: the journal carries the closed R-multiples the promote CLI reads.
    closed = [d for d in read_all_decisions(memory) if d.get("realized_pnl") is not None]
    assert len(closed) == 5
    assert all(realized_total(d) > 0 for d in closed)

    # Record the candidate lesson via the real record path.
    (cycle_dir(state, 200)).mkdir(parents=True, exist_ok=True)
    (cycle_dir(state, 200) / "reflection_output.json").write_text(json.dumps(
        {"lessons": [{"text": LESSON_TEXT, "polarity": "enabling", "regime": REGIME,
                      "tags": TAGS, "importance": 7}]}))
    record_lessons_cli.run(state, memory, 200, NOW, input_name="reflection_output")
    lid = read_lessons(memory)[0].id

    # Confirm 5x via promote_lesson_cli (the recurrence the eval harness drives). The 5th crosses the
    # count threshold; the statistical gate must also hold -> VALIDATED.
    out = {}
    for _ in range(5):
        out = promote_lesson_cli.run(memory, lid, "confirm")
    assert out["ok"] is True
    assert out["dsr_pvalue"] >= 0.95              # winning book -> mean R clearly > 0
    promoted = next(lz for lz in read_lessons(memory) if lz.id == lid)
    assert promoted.state == "validated"
    assert promoted.confirmations == 5

    # The VALIDATED standing rule is retrieved back into the next cycle.
    got = retrieve_lessons_cli.build(state, memory, cycle=201, now=NOW + timedelta(hours=4),
                                     regime=[REGIME, "social-heavy"], tags=TAGS, k=5)
    assert LESSON_TEXT in {lz["text"] for lz in got["lessons"]}


def test_loop_holds_candidate_without_statistical_support(tmp_path):
    """The promotion gate is NOT weakened: with NO closed-trade edge (p-value 0.0) the lesson stays
    CANDIDATE even after the count threshold is crossed — confirmations accrue but it cannot validate.
    (Guards that the loop can't ratchet an unproven hunch into a standing veto.)"""
    memory = tmp_path / "memory"
    state = tmp_path / "state"
    (cycle_dir(state, 300)).mkdir(parents=True, exist_ok=True)
    (cycle_dir(state, 300) / "reflection_output.json").write_text(json.dumps(
        {"lessons": [{"text": "unproven capitulation hunch", "polarity": "enabling",
                      "regime": REGIME, "tags": TAGS}]}))
    record_lessons_cli.run(state, memory, 300, NOW, input_name="reflection_output")
    lid = read_lessons(memory)[0].id

    out = {}
    for _ in range(5):
        out = promote_lesson_cli.run(memory, lid, "confirm")   # no closed book -> p == 0.0
    assert out["ok"] is True and out["dsr_pvalue"] == 0.0
    held = next(lz for lz in read_lessons(memory) if lz.id == lid)
    assert held.state == "candidate" and held.confirmations == 5
