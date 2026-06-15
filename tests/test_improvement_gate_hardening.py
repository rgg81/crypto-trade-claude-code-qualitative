"""Hardening tests for the OVERSEER safety gate (futures_fund.improvement + repair).

These pin the SAFETY INVARIANT at runtime, not just on the shipped static fixture:

* Finding 1 — the ImprovementProposal model must REJECT a lying proposal (one whose
  self-asserted classification/safe_to_autofix disagrees with the deterministic
  classify_target()/can_autofix()), and there must be an apply helper that re-derives the
  gate at apply time and REFUSES a protected target regardless of the boolean.
* Finding 2 — the protected / risk-critical match must be CASE-INSENSITIVE (a
  case-insensitive FS resolves ``Sentiment_Audit.py`` to the real module).
* Finding 3 — multi-dot spellings (``*.py.bak``, ``*.py.orig``, ``*.bak.py``, ``*.tpl.py``,
  ``*.pyc``) of a protected/risk stem must NOT be auto-fixable.
* Finding 4 — the risk-critical set must cover every futures_fund risk module
  (rr_floor, self_audit, portfolio_risk, cycle_io) and repair must protect the real
  output-writer (cycle_io), not the non-existent ``cycle``.

No network, no clock (timestamps injected), tmp dirs only.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from futures_fund.improvement import (
    RISK_CRITICAL,
    ImprovementProposal,
    apply_is_allowed,
    can_autofix,
    classify_target,
)
from futures_fund.repair import is_protected

# --------------------------------------------------------------------------- #
# Finding 1 — runtime enforcement on the model + an apply gate                 #
# --------------------------------------------------------------------------- #


def test_lying_proposal_code_safe_on_auditor_is_rejected():
    """A proposal that mislabels the Auditor as code+safe must NOT be constructible."""
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(
            {
                "issue": "x",
                "evidence": [{"cycle": 1}],
                "target_file": "futures_fund/sentiment_audit.py",
                "classification": "code",        # LIE: it is protected
                "safe_to_autofix": True,          # LIE: can_autofix is False
                "fix_summary": "weaken check 2",
                "test_plan": "z",
            }
        )


def test_lying_proposal_code_safe_on_risk_gate_is_rejected():
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(
            {
                "issue": "x",
                "evidence": [{"cycle": 1}],
                "target_file": "futures_fund/risk_gate.py",
                "classification": "code",
                "safe_to_autofix": True,
                "fix_summary": "relax the floor",
                "test_plan": "z",
            }
        )


def test_proposal_classification_must_match_classify_target():
    """Even a non-protected target: a stated classification that disagrees with the
    deterministic classifier is rejected (no LLM-supplied free-text class)."""
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(
            {
                "issue": "x",
                "evidence": [{"cycle": 1}],
                "target_file": "agents/flow_sentiment.md",
                "classification": "config",       # LIE: it is a prompt
                "safe_to_autofix": True,
                "fix_summary": "y",
                "test_plan": "z",
            }
        )


def test_proposal_safe_true_on_protected_classification_is_rejected():
    """safe_to_autofix=True with classification=protected is internally contradictory."""
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(
            {
                "issue": "x",
                "evidence": [{"cycle": 1}],
                "target_file": "futures_fund/sentiment_audit.py",
                "classification": "protected",
                "safe_to_autofix": True,          # contradiction
                "fix_summary": "y",
                "test_plan": "z",
            }
        )


def test_honest_protected_proposal_still_validates():
    """An honest protected finding (surfaced, not safe) is still well-formed."""
    p = ImprovementProposal.model_validate(
        {
            "issue": "auditor advisory recurs",
            "evidence": [{"cycle": 7, "agent": "narrative"}],
            "target_file": "futures_fund/sentiment_audit.py",
            "classification": "protected",
            "safe_to_autofix": False,
            "fix_summary": "surface to human",
            "test_plan": "human review",
        }
    )
    assert p.classification == "protected"
    assert p.safe_to_autofix is False


def test_honest_prompt_proposal_still_validates():
    p = ImprovementProposal.model_validate(
        {
            "issue": "flow lane hallucinated",
            "evidence": [{"cycle": 42, "agent": "flow"}],
            "target_file": "agents/flow_sentiment.md",
            "classification": "prompt",
            "safe_to_autofix": True,
            "fix_summary": "tighten cite rule",
            "test_plan": "re-run agent example",
        }
    )
    assert p.classification == "prompt"
    assert p.safe_to_autofix is True


def test_safe_to_autofix_is_advisory_apply_gate_is_authoritative():
    """apply_is_allowed re-derives can_autofix at apply time and IGNORES a (forged) boolean.

    A protected target must be refused even if a caller hands in safe=True; a normal code
    target is allowed even if safe=False (the gate, not the boolean, decides)."""
    # protected target, lying boolean -> refused
    assert apply_is_allowed("futures_fund/sentiment_audit.py", claimed_safe=True) is False
    assert apply_is_allowed("futures_fund/risk_gate.py", claimed_safe=True) is False
    assert apply_is_allowed("futures_fund/rr_floor.py", claimed_safe=True) is False
    # autofixable target -> allowed regardless of the boolean
    assert apply_is_allowed("agents/flow_sentiment.md", claimed_safe=False) is True
    assert apply_is_allowed("config.yaml", claimed_safe=True) is True


def test_apply_is_allowed_accepts_a_proposal_and_ignores_its_flag():
    """apply_is_allowed accepts an ImprovementProposal and re-derives the gate from its target_file,
    not from the (recomputed-anyway) safe_to_autofix on the model."""
    prompt = ImprovementProposal.model_validate({
        "issue": "x", "evidence": [{"cycle": 1}],
        "target_file": "agents/flow_sentiment.md", "classification": "prompt",
        "safe_to_autofix": True, "fix_summary": "y", "test_plan": "z",
    })
    assert apply_is_allowed(prompt) is True
    protected = ImprovementProposal.model_validate({
        "issue": "x", "evidence": [{"cycle": 1}],
        "target_file": "futures_fund/sentiment_audit.py", "classification": "protected",
        "safe_to_autofix": False, "fix_summary": "surface", "test_plan": "human",
    })
    assert apply_is_allowed(protected) is False


# --------------------------------------------------------------------------- #
# Finding 5 — a path that escapes the project root is never autofixable         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", [
    "../../etc/passwd",
    "/etc/passwd",
    "futures_fund/../../escape.py",
    "../outside_repo/config.yaml",
])
def test_path_traversal_is_not_autofixable(path):
    assert can_autofix(path) is False, f"{path} escapes the project root — must be refused"
    assert apply_is_allowed(path) is False


def test_inside_root_relative_path_is_autofixable():
    # a normal in-tree relative path resolves inside the project root and is allowed.
    assert can_autofix("agents/flow_sentiment.md") is True
    assert can_autofix("futures_fund/reflect.py") is True


# --------------------------------------------------------------------------- #
# Finding 2 — case-insensitive protected matching                             #
# --------------------------------------------------------------------------- #

_PROTECTED_AND_RISK_STEMS = sorted(set(RISK_CRITICAL) | {
    "risk_gate", "executor", "exits", "consolidation", "policy",
    "liquidation", "sizing", "cycle_io",
})


@pytest.mark.parametrize("stem", _PROTECTED_AND_RISK_STEMS)
@pytest.mark.parametrize("speller", [str.upper, str.title, lambda s: s.capitalize()])
def test_case_variants_are_not_autofixable(stem, speller):
    path = f"futures_fund/{speller(stem)}.py"
    assert can_autofix(path) is False, f"{path} must be non-autofixable (case-insensitive)"
    assert classify_target(path) == "protected"


@pytest.mark.parametrize("path", [
    "futures_fund/Sentiment_Audit.py",
    "futures_fund/RISK_GATE.py",
    "futures_fund/SENTIMENT_AUDIT.py",
    "futures_fund/Risk_Gate.py",
])
def test_is_protected_case_insensitive(path):
    # risk_gate is in repair.PROTECTED_PATHS; sentiment_audit is risk-critical (not repair).
    assert can_autofix(path) is False


def test_repair_is_protected_case_insensitive():
    assert is_protected("futures_fund/RISK_GATE.py") is True
    assert is_protected("futures_fund/Executor.py") is True


# --------------------------------------------------------------------------- #
# Finding 3 — multi-dot / backup / template spellings evade .stem             #
# --------------------------------------------------------------------------- #

_MULTIDOT_RISK = [
    "futures_fund/risk_gate.py.bak",
    "futures_fund/risk_gate.py.orig",
    "futures_fund/risk_gate.bak.py",
    "futures_fund/sentiment_audit.tpl.py",
    "futures_fund/sentiment_audit.py.bak",
    "futures_fund/rr_floor.py.orig",
    "futures_fund/risk_gate.pyc",
    "futures_fund/executor.py.tmp",
]


@pytest.mark.parametrize("path", _MULTIDOT_RISK)
def test_multidot_protected_spellings_not_autofixable(path):
    assert can_autofix(path) is False, f"{path} (multi-dot of a protected stem) must be refused"
    assert classify_target(path) == "protected"


@pytest.mark.parametrize("path", _MULTIDOT_RISK)
def test_multidot_protected_spellings_protected_in_repair(path):
    assert is_protected(path) is True


def test_multidot_non_risk_file_still_autofixable():
    # a backup of a NON-risk module is still ordinary code (no false-positive on the guard).
    assert can_autofix("futures_fund/reflect.py.bak") is True
    assert can_autofix("scripts/decision_qa_cli.py.orig") is True


# --------------------------------------------------------------------------- #
# Finding 4 — the risk set covers every risk module; cycle_io is protected     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("module", ["rr_floor", "self_audit", "portfolio_risk", "cycle_io"])
def test_newly_protected_risk_modules_not_autofixable(module):
    path = f"futures_fund/{module}.py"
    assert can_autofix(path) is False, f"{path} owns/asserts a risk limit — must be non-autofixable"
    assert classify_target(path) == "protected"


def test_cycle_io_is_the_real_output_writer_and_is_protected():
    # repair previously protected the non-existent 'cycle' stem; the real writer is cycle_io.
    assert is_protected("futures_fund/cycle_io.py") is True


def test_every_futures_fund_module_mentioning_a_risk_limit_is_non_autofixable():
    """Fail-safe spot check: any futures_fund/*.py that enforces an RR / heat / risk limit
    must be non-autofixable. Anchors the policy so a new risk module is caught."""
    import re
    from pathlib import Path

    ff = Path(__file__).resolve().parent.parent / "futures_fund"
    risk_token = re.compile(r"HARD_MIN_RR|rr_floor|portfolio_heat|position_risk|BAND\b", re.I)
    offenders = []
    for py in ff.glob("*.py"):
        try:
            txt = py.read_text()
        except OSError:
            continue
        if risk_token.search(txt) and can_autofix(f"futures_fund/{py.name}"):
            offenders.append(py.name)
    assert not offenders, f"risk-limit modules left autofixable: {offenders}"
