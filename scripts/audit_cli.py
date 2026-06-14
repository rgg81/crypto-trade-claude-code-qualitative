"""The deterministic ANTI-HALLUCINATION AUDITOR CLI (Phase 8) — Operation ORACLE's hard gate.

    uv run python scripts/audit_cli.py --cycle N
    uv run python scripts/audit_cli.py --cycle N --now 2026-06-13T12:00:00Z

This is the every-cycle MANDATORY stage run BEFORE the execute boundary. It re-derives every
anti-hallucination check from the content store (GROUND TRUTH) via
:func:`futures_fund.sentiment_audit.review_cycle` and persists the resulting
:class:`~futures_fund.sentiment_audit.AuditVerdict` to ``state/cycle/<N>/auditor.json`` — exactly
where :func:`futures_fund.sentiment_audit.audit_gate_ok` reads it before ANY fill.

It loads three cycle artifacts:
  * ``state/cycle/<N>/sentiment_reads.json``  -> ``list[SentimentRead]``
  * ``state/cycle/<N>/plans.json`` (or per-coin ``plan_<COIN>.json``) -> ``list[ResearchPlan]``
  * ``state/cycle/<N>/proposals.json`` (``.proposals``)              -> ``list[AgentProposal]``

and the currently-degraded sources (INJECTED into the auditor, never imported there) via
:func:`futures_fund.source_health.degraded_sources`.

FAIL-CLOSED end to end: a MISSING or MALFORMED reads/plans/proposals file does NOT crash and does
NOT silently pass — it yields a FAILED :class:`AuditVerdict` (one synthetic ``inputs_loadable``
check) and that failed verdict is STILL WRITTEN to ``auditor.json``, so the execute gate halts as it
would on an explicit veto. Absence must halt as hard as a failed check.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.contracts import (
    AgentProposal,
    ResearchPlan,
    SentimentRead,
)
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.sentiment_audit import AuditCheck, AuditVerdict, review_cycle
from futures_fund.source_health import degraded_sources, load_health

_STATE_DIR = "state"
_CONTENT_DIR = "content"


def _parse_now(raw: str | None) -> datetime:
    """Injected decision clock; defaults to wall-clock UTC. Naive ISO is read as UTC."""
    if raw is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _fail_closed(reason: str) -> AuditVerdict:
    """A FAILED verdict carrying a single synthetic ``inputs_loadable`` check.

    Used whenever an audited input is missing or unparseable: the verdict's ``passed`` is False so
    :func:`audit_gate_ok` halts, and the human-readable cause rides along in the check's detail."""
    check = AuditCheck(name="inputs_loadable", passed=False, detail=reason)
    return AuditVerdict(passed=False, checks=[check], mismatches=["inputs_loadable"])


def _load_reads(cdir: Path) -> list[SentimentRead]:
    """Parse ``sentiment_reads.json`` -> list[SentimentRead]. Raises on missing/malformed (the
    caller maps any exception to a fail-closed verdict)."""
    path = cdir / "sentiment_reads.json"
    raw = json.loads(path.read_text())  # FileNotFoundError / JSONDecodeError -> caught upstream
    if not isinstance(raw, list):
        raise ValueError(f"sentiment_reads.json must be a JSON list, got {type(raw).__name__}")
    return [SentimentRead.model_validate(r) for r in raw]


def _load_plans(cdir: Path) -> list[ResearchPlan]:
    """Parse the decider's plans. Prefers a single ``plans.json`` (a JSON list); falls back to
    per-coin ``plan_<COIN>.json`` files (each a single ResearchPlan object). At least one source
    must exist — a cycle with NO plans file at all is fail-closed (the decider never ran)."""
    combined = cdir / "plans.json"
    if combined.exists():
        raw = json.loads(combined.read_text())
        if not isinstance(raw, list):
            raise ValueError(f"plans.json must be a JSON list, got {type(raw).__name__}")
        return [ResearchPlan.model_validate(p) for p in raw]

    per_coin = sorted(cdir.glob("plan_*.json"))
    if not per_coin:
        raise FileNotFoundError(
            f"no plans: neither {combined} nor any plan_<coin>.json under {cdir}"
        )
    plans: list[ResearchPlan] = []
    for p in per_coin:
        plans.append(ResearchPlan.model_validate(json.loads(p.read_text())))
    return plans


def _load_proposals(cdir: Path) -> list[AgentProposal]:
    """Parse ``proposals.json`` (``.proposals`` -> list[AgentProposal]). Raises on missing/malformed
    so the caller fails closed — a missing proposals file can never silently pass the gate."""
    path = cdir / "proposals.json"
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"proposals.json must be a JSON object, got {type(raw).__name__}")
    proposals = raw.get("proposals")
    if not isinstance(proposals, list):
        raise ValueError("proposals.json must carry a '.proposals' list")
    return [AgentProposal.model_validate(p) for p in proposals]


def run(
    state_dir,
    content_dir,
    cycle: int,
    now: datetime,
) -> AuditVerdict:
    """Load this cycle's audited artifacts, run :func:`review_cycle`, persist + return the verdict.

    FAIL-CLOSED: any missing/malformed reads/plans/proposals file is converted into a FAILED verdict
    (never raised), and that failed verdict is STILL written to ``auditor.json`` so the execute gate
    reads a halt rather than nothing. The degraded-source set is INJECTED here from source-health —
    the auditor itself never imports the health subsystem."""
    cdir = cycle_dir(state_dir, cycle)

    try:
        reads = _load_reads(cdir)
        plans = _load_plans(cdir)
        proposals = _load_proposals(cdir)
    except Exception as exc:  # noqa: BLE001 — fail-closed: ANY load error -> a written FAILED verdict
        verdict = _fail_closed(f"{type(exc).__name__}: {exc}")
        save_output(state_dir, cycle, "auditor", verdict)
        return verdict

    # Degraded sources are INJECTED (the auditor never imports source_health). Health load is itself
    # fail-soft — an unreadable health file degrades to "nothing degraded" rather than aborting the
    # audit, since the nine ground-truth checks do not depend on health being present.
    try:
        degraded = degraded_sources(load_health(state_dir), now)
    except Exception:  # noqa: BLE001
        degraded = set()

    verdict = review_cycle(
        content_dir,
        reads=reads,
        plans=plans,
        proposals=proposals,
        now=now,
        degraded_sources=degraded,
    )
    save_output(state_dir, cycle, "auditor", verdict)
    return verdict


def _print_verdict(cycle: int, verdict: AuditVerdict) -> None:
    """Human-readable PASS/FAIL line + the failing checks (name + detail)."""
    status = "PASS" if verdict.passed else "FAIL"
    print(f"AUDITOR cycle {cycle}: {status}")
    if not verdict.passed:
        print(f"failing checks: {', '.join(verdict.mismatches) or '(none named)'}")
        for c in verdict.checks:
            if not c.passed:
                print(f"  - {c.name}: {c.detail}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the deterministic anti-hallucination auditor for one cycle (Phase 8)."
    )
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--content-dir", default=_CONTENT_DIR)
    ap.add_argument("--now", default=None, help="ISO timestamp (UTC); defaults to now")
    args = ap.parse_args(argv)

    verdict = run(args.state_dir, args.content_dir, args.cycle, _parse_now(args.now))
    _print_verdict(args.cycle, verdict)
    # Non-zero exit on a failed verdict so a wrapping ladder HALTS here; the persisted auditor.json
    # is the authoritative gate flag either way.
    return 0 if verdict.passed else 2


if __name__ == "__main__":
    sys.exit(main())
