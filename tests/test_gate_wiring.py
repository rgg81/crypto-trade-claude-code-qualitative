"""Offline wiring tests for scripts/gate_execute_cli.gate_execute — the Phase-9 deterministic risk
gate + consolidation + execution + journal of the qualitative sentiment desk.

Everything is injected (context.json carries every price/spec/funding figure; the auditor verdict is
seeded on disk) so the whole pipeline runs with NO network and NO live exchange. The three crux
invariants (Rule 4):
  * a clean cycle (passing auditor + a valid proposal) OPENS a position, journals a Phase-1
    decision, and writes report.json carrying the candle/ran_at run-markers the scheduler reads;
  * a FAILED auditor opens NOTHING but still writes a report (reason "audit veto");
  * a proposal that violates the RR floor / liq-distance rule is VETOED (no open).
"""
from __future__ import annotations

from datetime import UTC, datetime

from futures_fund.config import Settings
from futures_fund.contracts import AgentProposal
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.journal import read_all_decisions
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.scheduling import cycle_due
from futures_fund.state import load_positions
from scripts.gate_execute_cli import gate_execute

NOW = datetime(2026, 6, 13, 12, 0, tzinfo=UTC)


def _settings() -> Settings:
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def _spec(symbol: str = "BTCUSDT") -> dict:
    return SymbolSpec(
        symbol=symbol, tick_size=0.01, step_size=0.001, min_notional=5.0,
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12,
                                 mmr=0.004, maint_amount=0.0, max_leverage=125.0)],
    ).model_dump()


def _context(symbol="BTCUSDT", mark=100.0, atr=2.0, funding=0.0,
             mood="greedy", dispersion=0.2) -> dict:
    return {
        "crowd_mood": {"mood": mood, "dispersion": dispersion},
        "symbols": {symbol: {"spec": _spec(symbol), "mark": mark, "atr": atr,
                             "funding_rate": funding}},
        "pnl": {"daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0},
        "scorecard": {"recent_hit_rate": 0.55},
    }


def _proposal(symbol="BTCUSDT", direction="long", entry=100.0, stop=96.0,
              tps=None, atr=2.0) -> dict:
    ap = AgentProposal(
        symbol=symbol, direction=direction, entry=entry, stop=stop,
        take_profits=tps if tps is not None else [entry + 12.0],
        atr=atr, confidence=0.7, rationale="strong bullish flow",
        falsifiable_prediction="BTC reclaims 105 within 8 candles",
    ).model_dump()
    ap["contributing_agents"] = ["flow", "narrative"]
    return ap


def _seed(state_dir, cycle, *, audit_passed: bool, context=None, proposals=None,
          management=None, triggers=None, cancel_triggers=None,
          blocked_proposals=None, blocked_key_present=True) -> None:
    """Write this cycle's context.json, proposals.json and auditor.json under the cycle dir.

    ``blocked_proposals`` seeds the per-proposal block list; ``blocked_key_present=False`` omits the
    key entirely so the gate's defensive (absent => []) read is exercised."""
    save_output(state_dir, cycle, "context", context if context is not None else _context())
    save_output(state_dir, cycle, "proposals", {
        "proposals": proposals if proposals is not None else [],
        "management": management or [],
        "triggers": triggers or [],
        "cancel_triggers": cancel_triggers or [],
    })
    auditor = {"passed": audit_passed, "checks": [],
               "mismatches": [] if audit_passed else ["evidence"]}
    if blocked_key_present:
        auditor["blocked_proposals"] = list(blocked_proposals or [])
    save_output(state_dir, cycle, "auditor", auditor)


def _run(state_dir, memory_dir, cycle, **kw):
    return gate_execute(str(state_dir), str(memory_dir), cycle, NOW,
                        settings=_settings(), exchange=None, **kw)


# --------------------------------------------------------------------------- #
# 1. CLEAN CYCLE: passing auditor + valid proposal -> open + journal + report  #
# --------------------------------------------------------------------------- #
def test_clean_cycle_opens_journals_and_writes_report(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, proposals=[_proposal()])
    report = _run(state_dir, memory_dir, 1)

    # opened exactly one position
    assert len(report["opened"]) == 1
    assert report["opened"][0]["symbol"] == "BTCUSDT" and report["opened"][0]["direction"] == "long"
    positions = load_positions(state_dir)
    assert len(positions) == 1 and positions[0].symbol == "BTCUSDT"

    # journaled a Phase-1 decision carrying the sentiment provenance
    decisions = read_all_decisions(memory_dir)
    assert len(decisions) == 1
    d = decisions[0]
    assert d["symbol"] == "BTCUSDT" and d["cycle"] == 1
    assert d["contributing_agents"] == ["flow", "narrative"]
    assert d["falsifiable_prediction"] == "BTC reclaims 105 within 8 candles"
    assert d["crowd_mood"] == "greedy"
    assert d["id"] == positions[0].decision_id   # the open is linked to its decision

    # report carries the scheduler run-markers
    assert "candle" in report and "ran_at" in report
    assert report["audit_ok"] is True and report["vetoed"] == []
    # ...and is persisted where cycle_due reads it
    assert (cycle_dir(str(state_dir), 1) / "report.json").exists()


def test_clean_cycle_report_satisfies_cycle_due(tmp_path):
    # the report's candle/ran_at must let scheduling.cycle_due see this candle as SERVED (SKIP)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, proposals=[_proposal()])
    _run(state_dir, memory_dir, 1)
    mode, _n, _why = cycle_due(str(state_dir), NOW)
    assert mode == "SKIP"   # cycle 1 served the candle containing NOW


# --------------------------------------------------------------------------- #
# 2. FAILED AUDITOR: opens NOTHING but still writes a report                   #
# --------------------------------------------------------------------------- #
def test_failed_auditor_opens_nothing_but_writes_report(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=False, proposals=[_proposal()])
    report = _run(state_dir, memory_dir, 1)

    assert report["opened"] == []
    assert report["audit_ok"] is False and report["reason"] == "audit veto"
    assert load_positions(state_dir) == []
    assert read_all_decisions(memory_dir) == []     # nothing journaled on a veto
    assert (cycle_dir(str(state_dir), 1) / "report.json").exists()
    assert any("AUDIT VETO" in w for w in report["warnings"])


def test_missing_auditor_fails_closed(tmp_path):
    # no auditor.json at all == fail-closed (absence is as hard a veto as an explicit fail)
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    save_output(state_dir, 1, "context", _context())
    save_output(state_dir, 1, "proposals", {"proposals": [_proposal()]})
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == [] and report["audit_ok"] is False
    assert load_positions(state_dir) == []


# --------------------------------------------------------------------------- #
# 3. RR / liq-distance violation -> VETOED, no open                            #
# --------------------------------------------------------------------------- #
def test_sub_rr_proposal_is_vetoed_no_open(tmp_path):
    # RR = reward/risk = (101-100)/(100-96) = 0.25 << 2.0 floor -> vetoed
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True,
          proposals=[_proposal(entry=100.0, stop=96.0, tps=[101.0])])
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == []
    assert len(report["vetoed"]) == 1
    assert report["vetoed"][0]["symbol"] == "BTCUSDT"
    assert "RR" in report["vetoed"][0]["reason"]
    assert load_positions(state_dir) == []
    assert read_all_decisions(memory_dir) == []


def test_liq_distance_violation_is_vetoed(tmp_path):
    # a VERY WIDE stop (45% of entry) makes the stop gap so large that even at 1x leverage the
    # liquidation price sits closer than the MIN_LIQ_DISTANCE_MULT (2.5x) rule allows -> the gate
    # cannot satisfy liq distance within the leverage cap -> veto (no open). RR is kept far above
    # the floor (reward 135 / risk 45 = 3.0) so the ONLY reason is the liq-distance rule.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True,
          proposals=[_proposal(entry=100.0, stop=55.0, tps=[235.0])])
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == []
    assert len(report["vetoed"]) == 1
    assert "liq distance" in report["vetoed"][0]["reason"] \
        or "liq-distance" in report["vetoed"][0]["reason"]
    assert load_positions(state_dir) == []


# --------------------------------------------------------------------------- #
# Management review: close + reduce execute (even under an audit veto)         #
# --------------------------------------------------------------------------- #
def _seed_holding(state_dir):
    from futures_fund.state import Position, save_positions
    save_positions(state_dir, [Position(
        symbol="BTCUSDT", direction="long", qty=1.0, entry=100.0, stop=95.0,
        take_profits=[120.0], leverage=5.0, margin=20.0, liq_price=82.0,
        opened_cycle=0, opened_ts=NOW, decision_id="held1")])


def test_management_close_realizes_and_removes_position(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed_holding(state_dir)
    _seed(state_dir, 1, audit_passed=True, proposals=[],
          management=[{"symbol": "BTCUSDT", "action": "close"}])
    report = _run(state_dir, memory_dir, 1)
    assert "BTCUSDT" not in {p.symbol for p in load_positions(state_dir)}
    assert len(report["closed"]) == 1 and report["closed"][0]["reason"] == "management_close"


def test_management_reduce_trims_qty(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed_holding(state_dir)
    _seed(state_dir, 1, audit_passed=True, proposals=[],
          management=[{"symbol": "BTCUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    report = _run(state_dir, memory_dir, 1)
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].qty == 0.5
    assert report["closed"][0]["reason"] == "management_reduce"


def test_management_close_skipped_on_nonpositive_mark(tmp_path):
    # MONEY-SAFETY: a context mark of 0 (a data glitch) is not None — it must NOT close the
    # position at price 0 (a fabricated catastrophic loss). Treat a non-positive mark as missing:
    # SKIP the close, KEEP the position, and warn (fail-loud, Rule 6).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed_holding(state_dir)
    ctx = _context()
    ctx["symbols"]["BTCUSDT"]["mark"] = 0.0   # data glitch: a zero mark
    _seed(state_dir, 1, audit_passed=True, context=ctx, proposals=[],
          management=[{"symbol": "BTCUSDT", "action": "close"}])
    report = _run(state_dir, memory_dir, 1)
    # the position is KEPT (never booked at price 0) and the anomaly is warned.
    assert "BTCUSDT" in {p.symbol for p in load_positions(state_dir)}
    assert report["closed"] == []
    assert any("BTCUSDT" in w and "mark" in w for w in report["warnings"])


def test_management_reduce_skipped_on_negative_mark(tmp_path):
    # a negative mark is just as much a glitch as zero — the reduce must be skipped, qty unchanged.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed_holding(state_dir)
    ctx = _context()
    ctx["symbols"]["BTCUSDT"]["mark"] = -5.0
    _seed(state_dir, 1, audit_passed=True, context=ctx, proposals=[],
          management=[{"symbol": "BTCUSDT", "action": "reduce", "reduce_fraction": 0.5}])
    report = _run(state_dir, memory_dir, 1)
    pos = load_positions(state_dir)
    assert len(pos) == 1 and pos[0].qty == 1.0   # untrimmed — the reduce was skipped
    assert report["closed"] == []
    assert any("BTCUSDT" in w and "mark" in w for w in report["warnings"])


def test_close_runs_even_under_audit_veto(tmp_path):
    # a risk-DECREASING close must still execute when the auditor vetoed new entries
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed_holding(state_dir)
    _seed(state_dir, 1, audit_passed=False, proposals=[_proposal()],
          management=[{"symbol": "BTCUSDT", "action": "close"}])
    report = _run(state_dir, memory_dir, 1)
    assert report["audit_ok"] is False and report["opened"] == []
    assert "BTCUSDT" not in {p.symbol for p in load_positions(state_dir)}   # close still ran
    assert len(report["closed"]) == 1


# --------------------------------------------------------------------------- #
# Triggers: arm new (audit-gated) + cancel directed                            #
# --------------------------------------------------------------------------- #
def test_arms_new_trigger_when_audit_ok(tmp_path):
    from futures_fund.pending_orders import load_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, proposals=[], triggers=[
        {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry",
         "trigger_level": 110.0, "stop": 105.0, "take_profits": [130.0], "atr": 2.0}])
    report = _run(state_dir, memory_dir, 1)
    assert report["triggers_armed"] == 1
    orders = load_pending_orders(state_dir)
    assert any(o.symbol == "BTCUSDT" and o.kind == "stop_entry" for o in orders)


def test_audit_veto_does_not_arm_new_triggers(tmp_path):
    from futures_fund.pending_orders import load_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=False, proposals=[], triggers=[
        {"symbol": "BTCUSDT", "direction": "long", "kind": "stop_entry",
         "trigger_level": 110.0, "stop": 105.0, "take_profits": [130.0], "atr": 2.0}])
    report = _run(state_dir, memory_dir, 1)
    assert report["triggers_armed"] == 0
    assert load_pending_orders(state_dir) == []


def test_cancel_trigger_retires_armed_order(tmp_path):
    from futures_fund.pending_orders import PendingOrder, load_pending_orders, save_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="short", kind="stop_entry", trigger_level=80.0,
        stop=85.0, take_profits=[60.0], atr=2.0, created_cycle=0, expires_cycle=9)])
    _seed(state_dir, 1, audit_passed=True, proposals=[],
          cancel_triggers=[{"symbol": "BTCUSDT"}])
    report = _run(state_dir, memory_dir, 1)
    assert report["triggers_canceled"] == 1
    assert load_pending_orders(state_dir) == []


# --------------------------------------------------------------------------- #
# Decider-style triggers: a bare {symbol, direction, kind, level, risk_mult,   #
# reason} (no stop/tp/atr) must now ARM (gate maps/derives the missing fields) #
# instead of being SILENTLY DROPPED as 'malformed'. Genuinely-invalid triggers #
# (no symbol / no level / no ATR for a derived stop) are still dropped LOUD.    #
# --------------------------------------------------------------------------- #
def test_decider_style_trigger_arms_with_derived_geometry(tmp_path):
    # the cycle-2 DOGE regression: a valid-looking Decider trigger carrying ONLY
    # {symbol, direction, kind:"stop_entry", level, risk_mult, reason} (no stop/tp/atr,
    # `level` not `trigger_level`) was dropped "malformed" and armed 0 triggers. It must now ARM a
    # valid PendingOrder: level->trigger_level, atr from context, a ~1.5x-ATR stop, a >=2R TP.
    from futures_fund.pending_orders import load_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ctx = _context(symbol="DOGEUSDT", mark=0.16, atr=0.01)
    _seed(state_dir, 1, audit_passed=True, context=ctx, proposals=[], triggers=[
        {"symbol": "DOGEUSDT", "direction": "long", "kind": "stop_entry",
         "level": 0.18, "risk_mult": 0.5, "reason": "squeeze forming, arm on confirmation"}])
    report = _run(state_dir, memory_dir, 1)

    assert report["triggers_armed"] >= 1
    orders = load_pending_orders(state_dir)
    armed = [o for o in orders if o.symbol == "DOGEUSDT"]
    assert len(armed) == 1
    o = armed[0]
    assert o.kind == "stop_entry" and o.direction == "long"
    assert o.trigger_level == 0.18            # `level` mapped to trigger_level
    assert o.atr == 0.01                      # derived from context.json symbols[sym].atr
    assert o.stop < o.trigger_level           # long stop derived BELOW trigger (invalidating side)
    assert o.take_profits and o.take_profits[0] > o.trigger_level   # long TP above the trigger
    # the nearest TP is at/above the 2.0 RR floor from (level, stop) — never armed sub-floor.
    risk = o.trigger_level - o.stop
    assert (o.take_profits[0] - o.trigger_level) / risk >= 2.0 - 1e-9
    assert o.expires_cycle == 1 + 2           # current cycle + 2
    # persisted to the store the fire path reads.
    assert (state_dir / "pending_orders.json").exists()


def test_decider_trigger_explicit_geometry_is_honored(tmp_path):
    # defense-in-depth: when the Decider DOES supply stop/take_profits/atr, the gate uses THOSE
    # (back-fill only fires for omitted fields), so a sentiment-justified geometry survives.
    from futures_fund.pending_orders import load_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ctx = _context(symbol="DOGEUSDT", mark=0.16, atr=0.01)
    _seed(state_dir, 1, audit_passed=True, context=ctx, proposals=[], triggers=[
        {"symbol": "DOGEUSDT", "direction": "short", "kind": "stop_entry", "level": 0.14,
         "stop": 0.155, "take_profits": [0.10], "atr": 0.012, "risk_mult": 1.0,
         "reason": "euphoria flush, arm breakdown"}])
    report = _run(state_dir, memory_dir, 1)
    assert report["triggers_armed"] == 1
    o = [o for o in load_pending_orders(state_dir) if o.symbol == "DOGEUSDT"][0]
    assert o.stop == 0.155 and o.take_profits == [0.10] and o.atr == 0.012


def test_trigger_without_symbol_or_level_still_dropped(tmp_path):
    # the genuinely-malformed case stays a LOUD drop (0 armed): no symbol, and no level.
    from futures_fund.pending_orders import load_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, proposals=[], triggers=[
        {"direction": "long", "kind": "stop_entry", "risk_mult": 1.0, "reason": "no symbol/level"}])
    report = _run(state_dir, memory_dir, 1)
    assert report["triggers_armed"] == 0
    assert load_pending_orders(state_dir) == []
    assert any("trigger dropped (malformed)" in w for w in report["warnings"])


def test_trigger_dropped_when_no_atr_to_derive_stop(tmp_path):
    # a stop_entry with a level but NO stop and NO atr anywhere (trigger omits it, context has none)
    # cannot be made survivable -> dropped LOUD, never armed at a fabricated zero-ATR geometry.
    from futures_fund.pending_orders import load_pending_orders
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ctx = _context(symbol="DOGEUSDT", mark=0.16, atr=2.0)
    del ctx["symbols"]["DOGEUSDT"]["atr"]   # no ATR for the trigger's coin
    _seed(state_dir, 1, audit_passed=True, context=ctx, proposals=[], triggers=[
        {"symbol": "DOGEUSDT", "direction": "long", "kind": "stop_entry",
         "level": 0.18, "risk_mult": 1.0, "reason": "no atr to derive a stop"}])
    report = _run(state_dir, memory_dir, 1)
    assert report["triggers_armed"] == 0
    assert load_pending_orders(state_dir) == []
    assert any("DOGEUSDT" in w and "ATR" in w for w in report["warnings"])


def test_fired_decider_trigger_is_regated_not_auto_opened(tmp_path):
    # END-TO-END re-gate proof: arm a bare Decider trigger, fire it off a completed bar, then route
    # the FIRED order back through the SAME gate (AgentProposal -> to_trade_proposal -> evaluate). A
    # fired trigger is NEVER auto-opened: a sub-RR fired geometry is VETOED exactly like a fresh
    # proposal. (The derived geometry arms at >=2R, so we fire a HAND-ARMED sub-RR order to prove
    # the gate still rejects on fire — the fire path has no privileged auto-open.)
    from futures_fund.contracts import AgentProposal, to_trade_proposal
    from futures_fund.models import PortfolioHealth, SymbolSpec, mood_to_regime
    from futures_fund.pending_orders import (
        PendingOrder,
        check_pending_orders,
        fired_to_proposal,
        save_pending_orders,
    )
    from futures_fund.risk_gate import GateInputs, evaluate
    state_dir = tmp_path / "s"
    # a long stop_entry @100, stop 96 (risk 4), nearest TP 101 -> RR 0.25 << 2.0 floor.
    save_pending_orders(state_dir, [PendingOrder(
        symbol="BTCUSDT", direction="long", kind="stop_entry", trigger_level=100.0,
        stop=96.0, take_profits=[101.0], atr=2.0, created_cycle=0, expires_cycle=9)])
    fired, _expired, _remaining = check_pending_orders(
        str(state_dir), {"BTCUSDT": {"close": 101.0, "low": 99.0, "high": 102.0}}, 5)
    assert len(fired) == 1   # the break confirmed -> fires (but must still be GATED, not opened)

    # re-gate the fired order: it becomes a normal proposal and is RR-vetoed (no privileged open).
    prop = fired_to_proposal(fired[0])
    prop.update(confidence=0.7, horizon_hours=4.0)
    ap = AgentProposal.model_validate(prop)
    tp = to_trade_proposal(ap, 0.0)
    regime = mood_to_regime("neutral", 0.0)
    spec = SymbolSpec.model_validate(_spec("BTCUSDT"))
    health = PortfolioHealth(equity=10_000.0, peak_equity=10_000.0, open_heat=0.0,
                             recent_hit_rate=0.55)
    gi = GateInputs(proposal=tp, spec=spec, regime=regime, health=health,
                    open_positions=[], daily_pnl_pct=0.0, weekly_pnl_pct=0.0, monthly_pnl_pct=0.0)
    decision = evaluate(gi)
    assert decision.verdict == "veto" and decision.sized_trade is None   # re-gated, NOT auto-opened
    assert "RR" in decision.reason


# --------------------------------------------------------------------------- #
# Degenerate inputs are fail-soft (never crash the gate)                       #
# --------------------------------------------------------------------------- #
def test_missing_proposals_file_is_standdown(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    save_output(state_dir, 1, "context", _context())
    save_output(state_dir, 1, "auditor", {"passed": True})
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == [] and report["vetoed"] == []
    assert (cycle_dir(str(state_dir), 1) / "report.json").exists()


def test_proposal_without_spec_in_context_is_vetoed(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    ctx = _context()
    ctx["symbols"] = {}   # no per-symbol spec/mark for the proposed coin
    _seed(state_dir, 1, audit_passed=True, context=ctx, proposals=[_proposal()])
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == []
    assert len(report["vetoed"]) == 1 and "spec" in report["vetoed"][0]["reason"]


def test_malformed_proposal_is_vetoed_not_crash(tmp_path):
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    bad = {"symbol": "BTCUSDT", "direction": "long", "entry": 100.0, "stop": 105.0,  # stop>entry!
           "take_profits": [120.0], "atr": 2.0, "confidence": 0.7}
    _seed(state_dir, 1, audit_passed=True, proposals=[bad])
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == [] and len(report["vetoed"]) == 1
    assert "malformed" in report["vetoed"][0]["reason"]


def test_journaled_memory_ids_are_the_cycle_retrieved_set(tmp_path):
    # PROVENANCE: retrieved_memory_ids must be the lessons ACTUALLY retrieved for THIS cycle
    # (state/cycle/<N>/lessons.json), NOT every lesson in the store. Seed a store with two
    # lessons but a cycle retrieval that surfaced only one — the journal must cite only that one.
    from futures_fund.lessons import append_lesson
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    id_a = append_lesson(memory_dir, {"text": "lesson A", "polarity": "process"}, NOW)
    append_lesson(memory_dir, {"text": "lesson B", "polarity": "process"}, NOW)  # NOT retrieved
    # the retrieve_lessons_cli output for this cycle surfaced only lesson A.
    save_output(state_dir, 1, "lessons", {"lessons": [{"id": id_a, "text": "lesson A"}]})
    _seed(state_dir, 1, audit_passed=True, proposals=[_proposal()])
    _run(state_dir, memory_dir, 1)

    d = read_all_decisions(memory_dir)[0]
    assert d["retrieved_memory_ids"] == [id_a]   # only the retrieved one, not the whole store


def test_journaled_memory_ids_empty_when_no_cycle_lessons_file(tmp_path):
    # FAIL-SOFT: an absent lessons.json contributes an empty provenance list (never the store).
    from futures_fund.lessons import append_lesson
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    append_lesson(memory_dir, {"text": "lesson A", "polarity": "process"}, NOW)
    _seed(state_dir, 1, audit_passed=True, proposals=[_proposal()])   # no lessons.json written
    _run(state_dir, memory_dir, 1)

    d = read_all_decisions(memory_dir)[0]
    assert d["retrieved_memory_ids"] == []


def test_idempotent_rerun_does_not_double_open(tmp_path):
    # a DUE RETRY re-running the same cycle must not double-journal / double-open the same decision
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, proposals=[_proposal()])
    _run(state_dir, memory_dir, 1)
    _run(state_dir, memory_dir, 1)
    # journal is idempotent per (cycle, symbol, direction)
    assert len(read_all_decisions(memory_dir)) == 1


# --------------------------------------------------------------------------- #
# Per-proposal Auditor scoping: blocked_proposals skip the SPECIFIC proposal   #
# (audit gate passed, so the clean proposals still open).                      #
# --------------------------------------------------------------------------- #
def _two_symbol_context() -> dict:
    return {
        "crowd_mood": {"mood": "greedy", "dispersion": 0.2},
        "symbols": {
            "BTCUSDT": {"spec": _spec("BTCUSDT"), "mark": 100.0, "atr": 2.0,
                        "funding_rate": 0.0},
            "ETHUSDT": {"spec": _spec("ETHUSDT"), "mark": 100.0, "atr": 2.0,
                        "funding_rate": 0.0},
        },
        "pnl": {"daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0},
        "scorecard": {"recent_hit_rate": 0.55},
    }


def test_blocked_proposal_is_skipped_clean_ones_open(tmp_path):
    # audit PASSED (no fatal) but ETHUSDT is in blocked_proposals -> only BTCUSDT opens.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, context=_two_symbol_context(),
          proposals=[_proposal(symbol="BTCUSDT"), _proposal(symbol="ETHUSDT")],
          blocked_proposals=["ETHUSDT"])
    report = _run(state_dir, memory_dir, 1)

    opened_syms = {o["symbol"] for o in report["opened"]}
    assert opened_syms == {"BTCUSDT"}
    # the blocked one is recorded under vetoed with the audit-blocked reason.
    blocked = [v for v in report["vetoed"] if v["symbol"] == "ETHUSDT"]
    assert len(blocked) == 1
    assert "audit-blocked" in blocked[0]["reason"]
    assert {p.symbol for p in load_positions(state_dir)} == {"BTCUSDT"}


def test_blocked_list_absent_opens_all_clean(tmp_path):
    # auditor.json with NO blocked_proposals key at all (defensive read => []) -> both open.
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, context=_two_symbol_context(),
          proposals=[_proposal(symbol="BTCUSDT"), _proposal(symbol="ETHUSDT")],
          blocked_key_present=False)
    report = _run(state_dir, memory_dir, 1)
    assert {o["symbol"] for o in report["opened"]} == {"BTCUSDT", "ETHUSDT"}
    assert report["vetoed"] == []


def test_fatal_verdict_opens_nothing_even_with_block_list(tmp_path):
    # a FATAL verdict (passed False) opens NOTHING — unchanged fail-closed — regardless of the
    # blocked list (it never gets consulted because no proposal is iterated).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=False, context=_two_symbol_context(),
          proposals=[_proposal(symbol="BTCUSDT"), _proposal(symbol="ETHUSDT")],
          blocked_proposals=["ETHUSDT"])
    report = _run(state_dir, memory_dir, 1)
    assert report["opened"] == []
    assert report["audit_ok"] is False and report["reason"] == "audit veto"
    assert load_positions(state_dir) == []


def test_blocked_proposal_not_journaled(tmp_path):
    # a blocked proposal must never journal a decision (it was skipped before execution).
    state_dir, memory_dir = tmp_path / "s", tmp_path / "m"
    _seed(state_dir, 1, audit_passed=True, context=_two_symbol_context(),
          proposals=[_proposal(symbol="BTCUSDT"), _proposal(symbol="ETHUSDT")],
          blocked_proposals=["ETHUSDT"])
    _run(state_dir, memory_dir, 1)
    decisions = read_all_decisions(memory_dir)
    assert {d["symbol"] for d in decisions} == {"BTCUSDT"}


# --------------------------------------------------------------------------- #
# UNGROUNDED-THESIS BACKSTOP: a held position whose direction no longer has a  #
# fresh grounded SentimentRead is auto-CLOSED, never coasted (the XRP bug).    #
# This net only ever CLOSES (risk-reducing), weakens no risk limit.            #
# --------------------------------------------------------------------------- #
from futures_fund.content_store import ContentItem, make_id, store_items  # noqa: E402
from futures_fund.state import Position, save_positions  # noqa: E402

XRP_CTX = {
    "crowd_mood": {"mood": "neutral", "dispersion": 0.2},
    "symbols": {"XRPUSDT": {"spec": _spec("XRPUSDT"), "mark": 2.0, "atr": 0.05,
                            "funding_rate": 0.0}},
    "pnl": {"daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0},
    "scorecard": {"recent_hit_rate": 0.55},
}


def _xrp_ctx(mark=2.0) -> dict:
    import copy
    ctx = copy.deepcopy(XRP_CTX)
    ctx["symbols"]["XRPUSDT"]["mark"] = mark
    return ctx


def _seed_xrp_short(state_dir, *, opened_cycle=0, memory_dir=None):
    """A held XRP SHORT opened a PRIOR cycle (opened_cycle defaults to 0). When ``memory_dir`` is
    given, also seed the matching Phase-1 journal decision (id 'xrp1') so its close can be
    patched."""
    save_positions(state_dir, [Position(
        symbol="XRPUSDT", direction="short", qty=10.0, entry=2.0, stop=2.1,
        take_profits=[1.8], leverage=3.0, margin=6.6, liq_price=2.6,
        opened_cycle=opened_cycle, opened_ts=NOW, decision_id="xrp1")])
    if memory_dir is not None:
        from futures_fund.journal import append_decision
        append_decision(str(memory_dir), {
            "id": "xrp1", "ts": NOW, "cycle": opened_cycle, "symbol": "XRPUSDT",
            "direction": "short", "entry": 2.0, "stop": 2.1})


def _store_item(content_dir, *, coin="XRP", source="coindesk") -> str:
    """Seed ONE content item for `coin`; return its id (so a read can cite an id that RESOLVES)."""
    item = ContentItem(
        id=make_id(source, f"https://{source}.example/{coin.lower()}-a",
                   f"{coin} sentiment item"),
        source=source, feed=f"https://{source}.example/rss",
        url=f"https://{source}.example/{coin.lower()}-a",
        title=f"{coin} sentiment item", body="", coins=[coin],
        published_ts=NOW, fetched_ts=NOW)
    store_items(str(content_dir), [item])
    return item.id


def _read(coin, stance, s, *, item_ids, confidence=0.7) -> dict:
    return {
        "agent": "flow", "coin": coin, "stance": stance, "level": "negative",
        "s": s, "confidence": confidence,
        "claims": [{"text": "tone", "item_ids": list(item_ids), "coins": [coin]}],
        "rationale": "crowd read", "as_of_ts": NOW.isoformat(),
    }


def _seed_reads(state_dir, cycle, reads):
    save_output(state_dir, cycle, "sentiment_reads", reads)


def _run_xrp(state_dir, memory_dir, content_dir, cycle):
    return gate_execute(str(state_dir), str(memory_dir), cycle, NOW,
                        settings=_settings(), exchange=None, content_dir=str(content_dir))


def test_held_short_with_no_bearish_read_is_autoclosed(tmp_path):
    # the XRP-bug case: a SHORT held a PRIOR cycle whose direction has NO fresh bearish grounded
    # read for XRP must be CLOSED, not coasted — even though the Decider emitted no close (HOLD).
    state_dir, memory_dir, content_dir = tmp_path / "s", tmp_path / "m", tmp_path / "c"
    _seed_xrp_short(state_dir, memory_dir=memory_dir)
    # reads for THIS cycle: a BULLISH XRP read (the short's bearish direction is unsupported); it
    # cites a resolvable item, so the short is ungrounded purely on direction (not on evidence).
    iid = _store_item(content_dir, coin="XRP")
    _seed(state_dir, 1, audit_passed=True, context=_xrp_ctx(), proposals=[],
          management=[{"symbol": "XRPUSDT", "action": "hold"}])
    _seed_reads(state_dir, 1, [_read("XRP", "bullish", 0.5, item_ids=[iid])])
    report = _run_xrp(state_dir, memory_dir, content_dir, 1)

    assert "XRPUSDT" not in {p.symbol for p in load_positions(state_dir)}   # auto-closed
    auto = [c for c in report["closed"] if "ungrounded thesis" in c.get("reason", "")]
    assert len(auto) == 1 and auto[0]["symbol"] == "XRPUSDT"
    # the journal outcome for the held decision was patched (exit booked).
    patched = [d for d in read_all_decisions(memory_dir)
               if d.get("id") == "xrp1" and d.get("realized_pnl") is not None]
    assert len(patched) == 1


def test_held_short_with_bearish_grounded_read_is_kept(tmp_path):
    # a SHORT held a PRIOR cycle WITH a fresh BEARISH read citing a RESOLVABLE item is GROUNDED ->
    # the backstop keeps it (the thesis still holds).
    state_dir, memory_dir, content_dir = tmp_path / "s", tmp_path / "m", tmp_path / "c"
    _seed_xrp_short(state_dir)
    iid = _store_item(content_dir, coin="XRP")
    _seed(state_dir, 1, audit_passed=True, context=_xrp_ctx(), proposals=[])
    _seed_reads(state_dir, 1, [_read("XRP", "bearish", -0.5, item_ids=[iid])])
    report = _run_xrp(state_dir, memory_dir, content_dir, 1)

    assert "XRPUSDT" in {p.symbol for p in load_positions(state_dir)}   # KEPT — grounded
    assert [c for c in report["closed"] if "ungrounded" in c.get("reason", "")] == []


def test_held_short_bearish_read_with_unresolvable_id_is_autoclosed(tmp_path):
    # a bearish read whose cited item_id does NOT resolve in the store does NOT ground the position
    # (a thesis resting on a hallucinated/evicted item is ungrounded) -> auto-closed.
    state_dir, memory_dir, content_dir = tmp_path / "s", tmp_path / "m", tmp_path / "c"
    _seed_xrp_short(state_dir)
    _store_item(content_dir, coin="XRP")  # store SOMETHING, but the read cites a different id
    _seed(state_dir, 1, audit_passed=True, context=_xrp_ctx(), proposals=[])
    _seed_reads(state_dir, 1, [_read("XRP", "bearish", -0.5, item_ids=["does-not-exist"])])
    report = _run_xrp(state_dir, memory_dir, content_dir, 1)

    assert "XRPUSDT" not in {p.symbol for p in load_positions(state_dir)}
    assert any("ungrounded thesis" in c.get("reason", "") for c in report["closed"])


def test_position_opened_this_cycle_is_never_autoclosed(tmp_path):
    # a position OPENED in the CURRENT cycle is never coasting -> never auto-closed, even when no
    # read grounds its direction. Open a fresh XRP SHORT this cycle; provide a BULLISH XRP read
    # (would be ungrounded for a short) — the brand-new open must survive (opened_cycle == cycle).
    state_dir, memory_dir, content_dir = tmp_path / "s", tmp_path / "m", tmp_path / "c"
    iid = _store_item(content_dir, coin="XRP")
    # a sub-2-mark short proposal: entry 2.0, stop 2.1 (loss-side above), TP 1.6 (>=2R).
    prop = _proposal(symbol="XRPUSDT", direction="short", entry=2.0, stop=2.1,
                     tps=[1.6], atr=0.05)
    _seed(state_dir, 1, audit_passed=True, context=_xrp_ctx(), proposals=[prop])
    _seed_reads(state_dir, 1, [_read("XRP", "bullish", 0.5, item_ids=[iid])])
    report = _run_xrp(state_dir, memory_dir, content_dir, 1)

    assert len(report["opened"]) == 1 and report["opened"][0]["symbol"] == "XRPUSDT"
    assert "XRPUSDT" in {p.symbol for p in load_positions(state_dir)}   # NOT auto-closed
    assert [c for c in report["closed"] if "ungrounded" in c.get("reason", "")] == []


def test_ungrounded_autoclose_skipped_on_nonpositive_mark(tmp_path):
    # MONEY-SAFETY: an ungrounded held position must NOT be auto-closed at a non-positive mark (a
    # close at price ~0 books a fabricated catastrophic loss). SKIP, KEEP, warn.
    state_dir, memory_dir, content_dir = tmp_path / "s", tmp_path / "m", tmp_path / "c"
    _seed_xrp_short(state_dir)
    iid = _store_item(content_dir, coin="XRP")
    _seed(state_dir, 1, audit_passed=True, context=_xrp_ctx(mark=0.0), proposals=[])
    _seed_reads(state_dir, 1, [_read("XRP", "bullish", 0.5, item_ids=[iid])])  # ungrounded short
    report = _run_xrp(state_dir, memory_dir, content_dir, 1)

    assert "XRPUSDT" in {p.symbol for p in load_positions(state_dir)}   # KEPT (no close at 0)
    assert [c for c in report["closed"] if "ungrounded" in c.get("reason", "")] == []
    assert any("XRPUSDT" in w and "mark" in w for w in report["warnings"])


def test_ungrounded_backstop_skipped_when_reads_file_missing(tmp_path):
    # FAIL-SOFT: no sentiment_reads.json at all => the backstop is skipped entirely (close nothing,
    # never crash). The ungrounded short is KEPT because there is no read data to judge it on.
    state_dir, memory_dir, content_dir = tmp_path / "s", tmp_path / "m", tmp_path / "c"
    _seed_xrp_short(state_dir)
    _seed(state_dir, 1, audit_passed=True, context=_xrp_ctx(), proposals=[])
    # NB: no _seed_reads(...) — sentiment_reads.json is absent.
    report = _run_xrp(state_dir, memory_dir, content_dir, 1)

    assert "XRPUSDT" in {p.symbol for p in load_positions(state_dir)}   # KEPT — backstop skipped
    assert [c for c in report["closed"] if "ungrounded" in c.get("reason", "")] == []
