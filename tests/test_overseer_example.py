"""Conformance test for the OVERSEER meta-reviewer example fixture (agents/overseer.md output).

The overseer prompt promises strict JSON ``{proposals: [ImprovementProposal], summary}``. This pins
the shipped example to the :class:`~futures_fund.improvement.ImprovementProposal` contract AND to
the defining safety invariant: ``safe_to_autofix`` is true ONLY when the target is non-protected
(``classify_target`` != "protected") AND :func:`can_autofix` is true, and a ``protected`` target is
ALWAYS surfaced (never auto-fixed). No network, no clock — pure model_validate over static JSON.
"""
from __future__ import annotations

import json
from pathlib import Path

from futures_fund.improvement import ImprovementProposal, can_autofix, classify_target

FIX = Path(__file__).parent / "fixtures" / "agent_examples"


def _load() -> dict:
    return json.loads((FIX / "overseer.json").read_text())


def test_overseer_example_top_level_shape():
    data = _load()
    assert isinstance(data, dict)
    assert isinstance(data["proposals"], list) and data["proposals"], (
        "overseer example must carry a non-empty proposals list"
    )
    assert isinstance(data["summary"], str) and data["summary"]


def test_overseer_proposals_validate_against_contract():
    data = _load()
    proposals = [ImprovementProposal.model_validate(p) for p in data["proposals"]]
    assert proposals
    for p in proposals:
        assert p.issue and p.fix_summary and p.test_plan
        assert p.target_file
        assert p.classification in {"prompt", "config", "code", "protected"}


def test_overseer_safe_to_autofix_respects_the_gate():
    """The core safety invariant: safe_to_autofix is true ONLY when the target is non-protected
    AND can_autofix(target) is true; a protected target is always surfaced, never auto-fixed."""
    data = _load()
    for raw in data["proposals"]:
        p = ImprovementProposal.model_validate(raw)
        # the OVERSEER's stated classification must match the deterministic classifier.
        assert p.classification == classify_target(p.target_file), (
            f"{p.target_file}: stated classification {p.classification!r} disagrees with "
            f"classify_target {classify_target(p.target_file)!r}"
        )
        if p.safe_to_autofix:
            assert p.classification != "protected", (
                f"{p.target_file}: a protected target can NEVER be safe_to_autofix"
            )
            assert can_autofix(p.target_file) is True, (
                f"{p.target_file}: safe_to_autofix=True but can_autofix is False"
            )
        else:
            # not necessarily protected (could be a logged non-applied code fix), but a protected
            # target MUST land here.
            pass


def test_overseer_example_has_a_protected_finding_that_is_surfaced():
    """The example must demonstrate the NEVER-weaken-the-Auditor discipline: at least one finding
    targets a protected/risk file and is surfaced (safe_to_autofix False)."""
    data = _load()
    protected = [
        ImprovementProposal.model_validate(p)
        for p in data["proposals"]
        if classify_target(p["target_file"]) == "protected"
    ]
    assert protected, "overseer example must show a protected finding (surfaced, never auto-fixed)"
    for p in protected:
        assert p.safe_to_autofix is False
        assert can_autofix(p.target_file) is False


def test_overseer_every_proposal_is_grounded_in_evidence():
    """No speculation: every proposal cites concrete cycle/agent evidence."""
    data = _load()
    for raw in data["proposals"]:
        p = ImprovementProposal.model_validate(raw)
        assert p.evidence, f"{p.target_file}: every finding must be grounded in cited evidence"
        for ev in p.evidence:
            assert isinstance(ev, dict)
            assert "cycle" in ev, f"{p.target_file}: evidence ref must name a cycle"
