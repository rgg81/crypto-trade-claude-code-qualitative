"""Standing self-audit (Pillar 4 — AUDIT) for the qualitative sentiment desk.

A fast, deterministic panel of the desk's CRITICAL cross-module invariants, runnable any cycle / on
demand (``scripts/self_audit_cli.py``) as a cheap complement to the full test suite: it catches a
regression in a load-bearing SAFETY / DEPLOYMENT-pacing / ANTI-HALLUCINATION property without
running the 500+ regression tests. Every check is a hard invariant that MUST hold for the desk to be
safe to run.

This is written FRESH for ORACLE — the base desk's self_audit imported the DROPPED `playbook` and
`proposal_audit` modules (TA/regime-routing invariants that no longer exist). The qualitative panel
replaces them with the sentiment desk's own load-bearing properties:

  1. ANTI-MARTINGALE pacing — pacing NEVER presses while in drawdown (and DOES press when genuinely
     behind + healthy, so the de-risking guard can't be mistaken for a dead deployment path).
  2. GATE RR FLOOR — the adaptive reward:risk survival floor is present and never relaxes below the
     desk's HARD minimum across any regime quadrant.
  3. CONTENT-STORE INTEGRITY — every coin digest references items that actually resolve in the store
     (ground truth is self-consistent; no digest points at an evicted/hallucinated item).
  4. AUDITOR-GATE PRESENCE — the latest COMPLETED cycle (one that produced a ``report.json``) also
     carries an ``auditor.json`` verdict; a cycle that executed without the deterministic
     anti-hallucination gate having run is the cardinal failure mode.
  5. NO-PRICE-LEAK PATH — the evidence price card is plumbing-only (the non-directional disclaimer
     is stamped, never a signal) AND the auditor's price/TA-leak detector still vetoes TA language
     while keeping a clean sentiment rationale (direction stays 100% sentiment).

Checks 1–2 and 5b are PURE (no I/O). Checks 3–4 and 5a read the desk's REAL state/content store
(injected dirs, defaulting to ``state``/``content``) — there is no network and no live clock.
"""
from __future__ import annotations

from pathlib import Path

# The HARD reward:risk survival floor — the adaptive per-quadrant rr_floor may tune WITHIN its band
# but must never relax below this. Kept here (not in rr_floor) so self_audit is the single place
# that asserts the invariant; mirrors the base desk's gate RR floor (>= 2.0 seed, >= 1.6 band).
HARD_MIN_RR = 1.6

_STATE_DIR = "state"
_CONTENT_DIR = "content"


# --------------------------------------------------------------------------- #
# 1. ANTI-MARTINGALE pacing                                                    #
# --------------------------------------------------------------------------- #


def invariant_pacing_never_presses_in_drawdown() -> tuple[bool, str]:
    """In drawdown (>= CAUTION_DD) pacing must NEVER press — pressing would double risk into losses.

    Re-derives the pacing mode for a desk that is BEHIND pace AND under-deployed AND healthy enough
    to otherwise press, but in drawdown: the drawdown branch must force mode <= soft. The protected
    drawdown breakers own the loss path; pacing only ever spends UNUSED budget."""
    from futures_fund.pacing import CAUTION_DD, compute_pacing

    mode = compute_pacing(
        mtd_return=-0.04, days_elapsed=15, days_in_month=30,
        drawdown=CAUTION_DD + 0.01, open_heat=0.0,
    ).mode
    return mode != "press", f"in-drawdown mode={mode} (must NOT be press)"


def invariant_pacing_presses_when_behind_and_healthy() -> tuple[bool, str]:
    """The deployment path is LIVE: behind pace + under-deployed + no drawdown -> press.

    Without this companion check an always-soft (dead) pacing would pass check 1 vacuously. Here we
    prove pacing still DEPLOYS when it safely should — so check 1 is a real guard, not stuck."""
    from futures_fund.pacing import compute_pacing

    mode = compute_pacing(
        mtd_return=0.0, days_elapsed=15, days_in_month=30,
        drawdown=0.0, open_heat=0.0,
    ).mode
    return mode == "press", f"behind+healthy mode={mode} (must press)"


# --------------------------------------------------------------------------- #
# 2. GATE RR FLOOR                                                             #
# --------------------------------------------------------------------------- #


def invariant_rr_floor_at_or_above_hard_min(state_dir=_STATE_DIR) -> tuple[bool, str]:
    """The adaptive RR floor is PRESENT and never relaxes below ``HARD_MIN_RR`` in ANY quadrant.

    Re-derives ``effective_rr_floor`` for every quadrant from the persisted state (FAIL-SAFE to the
    2.0 seed when absent) and checks the rr_floor BAND's own hard lower bound — so neither the
    persisted state nor the module's clamp band can drop the survival floor below the desk minimum.
    """
    from futures_fund.rr_floor import BAND, QUADRANTS, effective_rr_floor, load_rr_floor

    band_lo = BAND[0]
    if band_lo < HARD_MIN_RR:
        return False, f"rr_floor BAND lower bound {band_lo} < HARD_MIN_RR {HARD_MIN_RR}"
    state = load_rr_floor(state_dir)
    floors = {q: effective_rr_floor(q, state) for q in QUADRANTS}
    bad = {q: v for q, v in floors.items() if v < HARD_MIN_RR}
    if bad:
        return False, "quadrant floors below HARD_MIN_RR: " + ", ".join(
            f"{q}={v:.2f}" for q, v in sorted(bad.items())
        )
    return True, f"all quadrant floors >= {HARD_MIN_RR} (band_lo={band_lo}); {floors}"


# --------------------------------------------------------------------------- #
# 3. CONTENT-STORE INTEGRITY                                                   #
# --------------------------------------------------------------------------- #


def invariant_digests_reference_existing_items(content_dir=_CONTENT_DIR) -> tuple[bool, str]:
    """Every ``top_item_ids`` entry in every coin digest must RESOLVE in the content store.

    Ground truth (the store) must be self-consistent: a digest pointing at an item that does not
    resolve (evicted by purge without the digest being recomputed, or hallucinated) is a broken
    reference the auditor's citation checks rest on. Re-resolves each id via ``get_item``. An
    empty / absent store is vacuously OK (a cold desk has nothing to violate)."""
    from futures_fund.content_store import _digests_dir, get_item

    ddir: Path = _digests_dir(content_dir)
    if not ddir.exists():
        return True, "no digests yet (cold store)"

    import json

    dangling: list[str] = []
    n_checked = 0
    for path in sorted(ddir.glob("*.json")):
        try:
            digest = json.loads(path.read_text())
        except (OSError, ValueError):
            dangling.append(f"{path.name}: unreadable digest")
            continue
        coin = digest.get("coin", path.stem)
        for iid in digest.get("top_item_ids", []) or []:
            n_checked += 1
            if get_item(content_dir, iid) is None:
                dangling.append(f"{coin}:{iid}")
    if dangling:
        return False, "digests reference missing items: " + ", ".join(dangling[:10])
    return True, f"all {n_checked} digest top-item references resolve"


# --------------------------------------------------------------------------- #
# 4. AUDITOR-GATE PRESENCE                                                     #
# --------------------------------------------------------------------------- #


def _completed_cycles(state_dir) -> list[int]:
    """Cycle numbers that produced a terminal ``report.json`` (the gate+execute step's output),
    newest last. A cycle with a report is one that REACHED execution — exactly the cycles that must
    have been gated by an auditor verdict first."""
    cdir = Path(state_dir) / "cycle"
    if not cdir.exists():
        return []
    out: list[int] = []
    for child in cdir.iterdir():
        if not child.is_dir() or not child.name.isdigit():
            continue
        if (child / "report.json").exists():
            out.append(int(child.name))
    return sorted(out)


def invariant_latest_completed_cycle_has_auditor(state_dir=_STATE_DIR) -> tuple[bool, str]:
    """The latest COMPLETED cycle (one with a ``report.json``) must carry an ``auditor.json``.

    A cycle that executed without the deterministic anti-hallucination gate having run is the
    cardinal failure mode — a hallucinated book could have reached fills. We find the highest cycle
    with a report and assert the auditor verdict file is present beside it. No completed cycle yet
    -> vacuously OK (nothing executed). (Presence is what's audited here; the verdict's own pass/
    fail is enforced fail-closed at execute time by ``sentiment_audit.audit_gate_ok``.)"""
    completed = _completed_cycles(state_dir)
    if not completed:
        return True, "no completed cycle yet (nothing executed)"
    latest = completed[-1]
    auditor = Path(state_dir) / "cycle" / str(latest) / "auditor.json"
    if not auditor.exists():
        return False, f"completed cycle {latest} has report.json but NO auditor.json (ungated)"
    return True, f"completed cycle {latest} carries an auditor.json"


# --------------------------------------------------------------------------- #
# 5. NO-PRICE-LEAK PATH                                                        #
# --------------------------------------------------------------------------- #


def invariant_price_card_is_non_directional() -> tuple[bool, str]:
    """The evidence price card is RISK PLUMBING ONLY — it always carries the non-directional note.

    Re-derives a price card via ``decision_io._build_price_card`` for both a working (injected)
    price_fn and a failing one: a successful card must stamp ``PRICE_CARD_NOTE`` (explicitly NOT a
    signal) and a failed fetch must degrade to the UNAVAILABLE note — never silently surface a mark
    as a directional input. Pure (the price_fn is injected; no network)."""
    from futures_fund.decision_io import (
        PRICE_CARD_NOTE,
        PRICE_CARD_UNAVAILABLE_NOTE,
        _build_price_card,
    )

    ok_card = _build_price_card("BTC", lambda _coin: (100.0, 2.0))
    bad_card = _build_price_card("BTC", lambda _coin: (_ for _ in ()).throw(RuntimeError("flaky")))
    ok = (
        ok_card.get("note") == PRICE_CARD_NOTE
        and bad_card.get("note") == PRICE_CARD_UNAVAILABLE_NOTE
        and bad_card.get("mark") is None
    )
    return ok, (
        f"ok_note={ok_card.get('note')!r} unavailable_note={bad_card.get('note')!r}"
    )


def invariant_auditor_vetoes_price_leak() -> tuple[bool, str]:
    """The auditor's price/TA-leak detector vetoes TA language but keeps a clean sentiment read.

    Direction must be 100% sentiment: a rationale citing 'support level' / 'RSI' / 'breakout level'
    is a leak and must trip ``_has_price_leak``; a pure-sentiment rationale must NOT. Re-derives
    both so a regex regression that stops catching leaks OR starts vetoing real sentiment is
    caught. Pure."""
    from futures_fund.sentiment_audit import _has_price_leak

    leaks = _has_price_leak("breaking $63k support level, RSI oversold, breakout level above")
    clean = _has_price_leak(
        "Whales are accumulating and the community mood is euphoric after the upgrade news."
    )
    ok = leaks and not clean
    return ok, f"leak_detected={leaks} clean_flagged={clean} (want leak=True clean=False)"


# --------------------------------------------------------------------------- #
# the standing panel                                                           #
# --------------------------------------------------------------------------- #


def _checks(state_dir=_STATE_DIR, content_dir=_CONTENT_DIR) -> list[tuple[str, bool, str]]:
    out: list[tuple[str, bool, str]] = []

    def add(name, result):
        ok, detail = result
        out.append((name, bool(ok), detail))

    # 1. ANTI-MARTINGALE pacing — never press in drawdown, but DO press when safely behind.
    add("pacing.anti_martingale", invariant_pacing_never_presses_in_drawdown())
    add("pacing.presses_when_behind", invariant_pacing_presses_when_behind_and_healthy())
    # 2. GATE RR FLOOR present and >= HARD_MIN_RR across all quadrants.
    add("gate.rr_floor", invariant_rr_floor_at_or_above_hard_min(state_dir))
    # 3. CONTENT-STORE INTEGRITY — digests reference items that resolve.
    add("content_store.digests_reference_existing_items",
        invariant_digests_reference_existing_items(content_dir))
    # 4. AUDITOR-GATE PRESENCE — latest completed cycle carries an auditor verdict.
    add("auditor.latest_completed_cycle_gated",
        invariant_latest_completed_cycle_has_auditor(state_dir))
    # 5. NO-PRICE-LEAK PATH — price card is plumbing-only + auditor still vetoes TA language.
    add("price.card_non_directional", invariant_price_card_is_non_directional())
    add("price.auditor_vetoes_leak", invariant_auditor_vetoes_price_leak())
    return out


def run_self_audit(state_dir=_STATE_DIR, content_dir=_CONTENT_DIR) -> dict:
    """Run the invariant panel over the desk's state/content store.

    Returns ``{ok, checks:[{name, ok, detail}]}``; ``ok`` is the AND of every invariant. `state_dir`
    / `content_dir` are injected so the panel is offline-testable against a crafted state."""
    results = _checks(state_dir, content_dir)
    return {
        "ok": all(ok for _, ok, _ in results),
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in results],
    }
