"""OVERSEER meta-reviewer SUPPORT — the deterministic pieces of the self-improvement engine.

The OVERSEER (``agents/overseer.md``, an Opus meta-reviewer) watches every cycle's SENTIMENT
ANALYSIS and agent reasoning for SYSTEMATIC problems — a lane hallucinating citations cycle after
cycle, ungrounded plans, miscalibrated confidence, redundant lanes echoing each other, sentiment
mislabels, coin mis-tags, recurring veto/advisory reasons, a lane whose reliability is decaying —
and emits :class:`ImprovementProposal`s naming the EXACT file to change. The orchestration workflow
(authored separately by the operator) drives the loop; THIS module supplies the offline, testable
machinery the loop leans on:

* :func:`can_autofix` — the SAFETY GATE. A fix may be auto-applied ONLY if its target is neither a
  protected module (re-using :func:`futures_fund.repair.is_protected`) NOR in the qualitative desk's
  risk-critical set (the Auditor included). Protected / risk files are ALWAYS surfaced to a human,
  NEVER auto-applied — the OVERSEER can never weaken a risk limit or the nine Auditor checks.
* :func:`classify_target` — ``prompt`` (``agents/*.md``) | ``config`` (``config.yaml``) |
  ``protected`` (risk / Auditor) | ``code`` (everything else).
* :func:`record_improvement` — appends an auditable, human-readable record to
  ``memory/improvement-journal.md`` AND a machine line to ``memory/improvement-journal.jsonl``
  (atomic; the TIMESTAMP IS PASSED IN — this module never reads the clock).
* :class:`ImprovementProposal` — the strict contract the OVERSEER emits per finding.

Everything here is DETERMINISTIC and OFFLINE: no network, no clock, no exchange. The self-
improvement loop must never be able to crash a cycle, so the journal writer is fail-soft on a torn
trailing line and atomic on every write.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from futures_fund.repair import is_protected

__all__ = [
    "ImprovementProposal",
    "RISK_CRITICAL",
    "apply_is_allowed",
    "can_autofix",
    "classify_target",
    "record_improvement",
]

# The qualitative desk's RISK-CRITICAL set — a "fix" here may NEVER be auto-applied; it can only be
# SURFACED to a human (spec rule §4: never weaken the Auditor's nine checks or any risk limit).
# `sentiment_audit` is the Auditor itself; the rest are the execution / risk / sizing plumbing.
# This OVERLAPS futures_fund.repair.PROTECTED_PATHS (risk_gate/executor/exits/consolidation/policy/
# liquidation/sizing/cycle/cycle_io) but ADDS the qualitative-desk modules that own or assert a risk
# limit yet are NOT on the core execution path: the Auditor (`sentiment_audit`), the reward/risk
# floor (`rr_floor`), the self-audit invariant checker (`self_audit`), the portfolio-heat accountant
# (`portfolio_risk`), the pending-order trigger-geometry + per-trade risk-reduction plumbing
# (`pending_orders`), the position-state / position-risk accountant (`state`) and the guardrail
# module itself (`improvement`) — the gate may NEVER auto-edit the gate. We re-use is_protected for
# the overlap and OR in this set so neither list can silently drift. The
# test_every_futures_fund_module_mentioning_a_risk_limit fail-safe anchors this: any module that
# references an RR / heat / position-risk / deadband token must be non-autofixable.
RISK_CRITICAL: tuple[str, ...] = (
    "risk_gate",
    "policy",
    "sizing",
    "liquidation",
    "consolidation",
    "executor",
    "exits",
    "sentiment_audit",
    "rr_floor",
    "self_audit",
    "portfolio_risk",
    "pending_orders",
    "state",
    "improvement",
    "repair",
)

_RISK_CRITICAL_CF = frozenset(r.casefold() for r in RISK_CRITICAL)

# The project root is the parent of this package directory. A fix target is only ever a path WITHIN
# this tree; anything that resolves outside it is path-traversal and is refused (finding 5).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _name_components(path: str) -> set[str]:
    """Every casefolded dotted component of the filename (mirrors repair._name_components).

    ``futures_fund/sentiment_audit.py``     -> {"sentiment_audit", "py"}
    ``futures_fund/sentiment_audit.py.bak``  -> {"sentiment_audit", "py", "bak"}
    ``futures_fund/Sentiment_Audit.py``      -> {"sentiment_audit", "py"}

    Splitting on EVERY dot defeats multi-dot evasion (``*.py.bak``, ``*.bak.py``, ``*.tpl.py``) and
    casefolding defeats a case-insensitive FS resolving ``Sentiment_Audit.py`` to the real module."""
    return {part.casefold() for part in Path(path).name.split(".") if part}


def _is_risk_critical(path: str) -> bool:
    """True if `path` is the Auditor or one of the risk / execution / sizing / portfolio modules.

    Case-insensitive and multi-dot aware: a protected name caught as ANY dotted component of the
    filename (so ``RR_Floor.py.bak`` is caught), not just :pymeth:`Path.stem`."""
    return bool(_name_components(path) & _RISK_CRITICAL_CF)


def _within_project_root(path: str) -> bool:
    """True if `path` resolves to a location INSIDE the project root.

    A relative path is resolved against the project root (where the desk lives), an absolute path
    against itself. ``../../etc/passwd`` and any absolute path outside the tree resolve OUTSIDE and
    are refused — a self-improvement fix may only touch files within the project."""
    try:
        p = Path(path)
        resolved = (p if p.is_absolute() else (_PROJECT_ROOT / p)).resolve()
    except (OSError, ValueError, RuntimeError):  # malformed path -> treat as escaping (refuse)
        return False
    return resolved == _PROJECT_ROOT or _PROJECT_ROOT in resolved.parents


def can_autofix(path: str) -> bool:
    """May a fix to `path` be AUTO-APPLIED (vs surfaced to a human)?

    True ONLY when `path`:

    * is a protected module (:func:`futures_fund.repair.is_protected`) -> never, OR
    * is in the desk's :data:`RISK_CRITICAL` set (incl. the Auditor) -> never, OR
    * escapes the project root (path traversal) -> never.

    All matching is CASE-INSENSITIVE and MULTI-DOT aware (a ``risk_gate.py.bak`` backup of a
    protected module is still refused). A protected / risk-critical / out-of-tree target ALWAYS
    returns False — it must be surfaced to a human and never auto-applied, so the OVERSEER can never
    weaken a risk limit or an Auditor check.
    """
    if is_protected(path):
        return False
    if _is_risk_critical(path):
        return False
    if not _within_project_root(path):
        return False
    return True


def classify_target(path: str) -> Literal["prompt", "config", "code", "protected"]:
    """Classify a fix target.

    * ``agents/*.md`` -> ``prompt`` (an agent's instruction prompt).
    * ``config.yaml`` -> ``config`` (the desk's runtime configuration).
    * a protected / risk-critical module (incl. the Auditor) -> ``protected``.
    * anything else -> ``code``.

    ``protected`` is checked BEFORE the ``.md`` / ``config`` shortcuts can never apply to it (the
    risk set is all ``.py``), but the order also documents intent: a risk file is protected first
    and foremost. A ``prompt`` / ``config`` / ``code`` classification is exactly the set
    :func:`can_autofix` clears (modulo path-traversal, which keeps its natural class but is refused
    by :func:`can_autofix`).
    """
    if is_protected(path) or _is_risk_critical(path):
        return "protected"
    p = Path(path)
    if p.suffix == ".md" and p.parent.name == "agents":
        return "prompt"
    if p.name == "config.yaml":
        return "config"
    return "code"


def apply_is_allowed(proposal_or_path: "ImprovementProposal | str", *,
                     claimed_safe: bool | None = None) -> bool:
    """The AUTHORITATIVE apply-time gate — re-derive the verdict, IGNORE any self-asserted flag.

    Accepts an :class:`ImprovementProposal` OR a path string. Returns True ONLY when
    :func:`can_autofix` is True for the target AND :func:`classify_target` is one of
    ``prompt`` / ``config`` / ``code`` (never ``protected``). The proposal's own
    ``safe_to_autofix`` (and any ``claimed_safe`` a caller hands in) is ADVISORY ONLY and is
    deliberately discarded here: the gate, not the boolean, decides. A protected / risk / out-of-tree
    target is refused even if the boolean says "safe"; a legitimate code target is allowed even if
    the boolean says "unsafe".
    """
    target = (
        proposal_or_path.target_file
        if isinstance(proposal_or_path, ImprovementProposal)
        else str(proposal_or_path)
    )
    # `claimed_safe` is accepted for call-site ergonomics but intentionally NOT consulted — the
    # whole point of the apply gate is that the LLM's self-assertion cannot move it.
    _ = claimed_safe
    return can_autofix(target) and classify_target(target) != "protected"


class ImprovementProposal(BaseModel):
    """One SYSTEMATIC finding the OVERSEER raises, naming the EXACT file to change.

    Every finding is GROUNDED in cited evidence (``evidence`` carries cycle / agent / item_id refs)
    — no speculation. ``safe_to_autofix`` is the OVERSEER's claim that the orchestrator may apply
    the change without a human; it is true ONLY when :func:`classify_target` is
    prompt/config/code AND :func:`can_autofix` is true. A protected / risk target is ALWAYS surfaced
    (``safe_to_autofix=False``, ``classification="protected"``).

    The model cannot self-assert its way past the gate: a ``@model_validator`` RE-DERIVES the
    deterministic verdict from ``target_file`` and REJECTS any proposal whose ``classification``
    disagrees with :func:`classify_target` or whose ``safe_to_autofix`` disagrees with
    :func:`can_autofix`. The runtime apply gate (:func:`apply_is_allowed`) is checked again at apply
    time, so even a hand-built model can never auto-apply a protected change.
    """

    model_config = ConfigDict(extra="forbid")

    issue: str                       # the systematic problem, stated concretely
    evidence: list[dict] = Field(default_factory=list)  # [{cycle, agent, item_id?, detail?}, ...]
    target_file: str                 # the EXACT file path to change
    classification: Literal["prompt", "config", "code", "protected"]
    fix_summary: str                 # the concrete change to make
    safe_to_autofix: bool            # true ONLY if classification is non-protected AND can_autofix
    test_plan: str                   # how to VERIFY the fix (tests to run / expectations)

    @model_validator(mode="after")
    def _enforce_deterministic_gate(self) -> "ImprovementProposal":
        """The proposal can NEVER self-assert its way past the gate.

        ``classification`` MUST equal :func:`classify_target(target_file)` and ``safe_to_autofix``
        is FORCIBLY RECOMPUTED as :func:`can_autofix(target_file)` — a model can never mark itself
        safe on a protected / risk-critical / out-of-tree path no matter what the LLM emitted. A
        ``classification`` that disagrees with the deterministic classifier is REJECTED (the field
        is not LLM free-text); a stale/forged ``safe_to_autofix`` is OVERWRITTEN rather than
        rejected so an honest-but-imprecise proposal still validates with the correct, safe value.
        """
        derived_class = classify_target(self.target_file)
        if self.classification != derived_class:
            raise ValueError(
                f"classification {self.classification!r} disagrees with classify_target("
                f"{self.target_file!r}) = {derived_class!r}"
            )
        derived_safe = can_autofix(self.target_file)
        # A proposal that self-asserts SAFE on a target the gate refuses is the dangerous lie — it
        # would auto-apply a protected / risk / out-of-tree change. REJECT it outright (a forged
        # safe=True on a protected path is internally contradictory and never well-formed).
        if self.safe_to_autofix and not derived_safe:
            raise ValueError(
                f"safe_to_autofix=True but can_autofix({self.target_file!r}) is False — a protected "
                f"/ risk-critical / out-of-tree target can never be auto-applied"
            )
        # Otherwise RECOMPUTE safe_to_autofix from ground truth so the model can never self-assert
        # safe on a protected path and an honest-but-imprecise flag is normalised to the safe value.
        # Assign via object.__setattr__ so we do not re-trigger validation.
        object.__setattr__(self, "safe_to_autofix", derived_safe)
        return self


# --------------------------------------------------------------------------- #
# improvement journal (auditable, atomic, dual-format)                         #
# --------------------------------------------------------------------------- #


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file in the same dir + os.replace (the content_store / cycle_io crash-safe pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _atomic_append_text(path: Path, addition: str) -> None:
    """Append `addition` to `path` ATOMICALLY (read-existing + concat + atomic replace).

    A plain ``open("a")`` is not atomic across a crash, so we rebuild the whole file and
    os.replace it — the file is always either the prior contents or prior+addition, never a
    truncated middle. Cheap for an append-only journal that grows a handful of lines per cycle.
    """
    prior = path.read_text() if path.exists() else ""
    _atomic_write_text(path, prior + addition)


def _atomic_append_jsonl(path: Path, rec: dict) -> None:
    """Append one JSON record as a line to `path`, self-healing a torn trailing fragment.

    If the existing file does NOT end in a newline (a previous crash truncated mid-line), prepend a
    newline so the fragment is left as its own garbage (tolerated) line rather than concatenated
    onto our clean record (mirrors decision_qa_cli._append_jsonl / the content_store pattern). The
    write itself is atomic."""
    prior = path.read_text() if path.exists() else ""
    sep = "" if (not prior or prior.endswith("\n")) else "\n"
    _atomic_write_text(path, prior + sep + json.dumps(rec, default=str) + "\n")


def record_improvement(memory_dir, entry: dict, ts: datetime) -> dict[str, Path]:
    """Append an auditable improvement record to BOTH journal files (atomic).

    Writes a human-readable, timestamped record to ``memory/improvement-journal.md`` AND a machine
    line to ``memory/improvement-journal.jsonl``. The TIMESTAMP IS PASSED IN (`ts`) — this function
    never calls the clock, so it is fully deterministic under test and the orchestration controls
    the cycle anchor.

    `entry` carries ``{detected, classification, target, fix_summary, applied: bool, test_result,
    surfaced: bool}``; any missing key is filled with a safe default (``applied``/``surfaced`` ->
    False, the rest -> ""), so a partial entry never raises. Returns the two written paths.
    """
    mem = Path(memory_dir)
    detected = str(entry.get("detected", ""))
    classification = str(entry.get("classification", ""))
    target = str(entry.get("target", ""))
    fix_summary = str(entry.get("fix_summary", ""))
    applied = bool(entry.get("applied", False))
    test_result = str(entry.get("test_result", ""))
    surfaced = bool(entry.get("surfaced", False))

    # ---- machine line (jsonl) ---------------------------------------------------------------- #
    rec = {
        "ts": ts.isoformat(),
        "detected": detected,
        "classification": classification,
        "target": target,
        "fix_summary": fix_summary,
        "applied": applied,
        "test_result": test_result,
        "surfaced": surfaced,
    }
    jsonl_path = mem / "improvement-journal.jsonl"
    _atomic_append_jsonl(jsonl_path, rec)

    # ---- human record (md) ------------------------------------------------------------------- #
    disposition = "auto-applied" if applied else ("surfaced to human" if surfaced else "logged")
    md = (
        f"\n## {ts:%Y-%m-%d %H:%M} improvement ({disposition})\n"
        f"- **Detected:** {detected}\n"
        f"- **Classification:** {classification}\n"
        f"- **Target:** {target}\n"
        f"- **Fix:** {fix_summary}\n"
        f"- **Applied:** {applied}\n"
        f"- **Surfaced:** {surfaced}\n"
        f"- **Test result:** {test_result}\n"
    )
    md_path = mem / "improvement-journal.md"
    _atomic_append_text(md_path, md)

    return {"md": md_path, "jsonl": jsonl_path}
