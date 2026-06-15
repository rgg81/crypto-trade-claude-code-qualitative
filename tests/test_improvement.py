"""Offline tests for the OVERSEER meta-reviewer SUPPORT module (futures_fund.improvement).

The OVERSEER is the autonomous "monitor all decisions + fix bugs" engine of the SELF-IMPROVEMENT
subsystem. This module supplies its deterministic, OFFLINE pieces: the safety gate that decides
whether a fix may be auto-applied (vs surfaced to a human), the target classifier, the
ImprovementProposal contract, and the auditable improvement-journal writer.

The defining invariant under test: a fix to a PROTECTED / risk-critical module is NEVER
auto-applicable — it can only be surfaced to a human. We re-use the SAME protected-module gate the
repair subsystem uses (futures_fund.repair.is_protected) and ADD the qualitative desk's
risk-critical set (the Auditor included), so the OVERSEER can never weaken a risk limit or the
nine Auditor checks. No network, no clock (timestamps are injected), tmp dirs only.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from futures_fund.improvement import (
    ImprovementProposal,
    can_autofix,
    classify_target,
    record_improvement,
)

# --------------------------------------------------------------------------- #
# can_autofix — the human-surfacing safety gate                                #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path",
    [
        "futures_fund/risk_gate.py",
        "futures_fund/policy.py",
        "futures_fund/sizing.py",
        "futures_fund/liquidation.py",
        "futures_fund/consolidation.py",
        "futures_fund/executor.py",
        "futures_fund/exits.py",
        "futures_fund/sentiment_audit.py",  # the Auditor itself — NEVER auto-fix
        "cycle.py",                          # repair.is_protected stem
    ],
)
def test_can_autofix_false_for_protected_and_risk(path):
    assert can_autofix(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "agents/flow_sentiment.md",
        "agents/narrative_sentiment.md",
        "config.yaml",
        "scripts/decision_qa_cli.py",
        "futures_fund/decision_qa.py",   # a normal (non-risk) code module
        "futures_fund/reflect.py",
    ],
)
def test_can_autofix_true_for_prompt_config_and_normal_code(path):
    assert can_autofix(path) is True


def test_can_autofix_reuses_repair_is_protected():
    # whatever futures_fund.repair.is_protected flags must ALSO be non-autofixable here.
    from futures_fund.repair import PROTECTED_PATHS, is_protected

    for stem in PROTECTED_PATHS:
        assert is_protected(f"futures_fund/{stem}.py") is True
        assert can_autofix(f"futures_fund/{stem}.py") is False


# --------------------------------------------------------------------------- #
# classify_target                                                              #
# --------------------------------------------------------------------------- #


def test_classify_target_prompt():
    assert classify_target("agents/flow_sentiment.md") == "prompt"
    assert classify_target("agents/auditor.md") == "prompt"


def test_classify_target_config():
    assert classify_target("config.yaml") == "config"
    assert classify_target("/abs/path/config.yaml") == "config"


def test_classify_target_protected():
    assert classify_target("futures_fund/risk_gate.py") == "protected"
    assert classify_target("futures_fund/sentiment_audit.py") == "protected"
    assert classify_target("cycle.py") == "protected"


def test_classify_target_code():
    assert classify_target("scripts/decision_qa_cli.py") == "code"
    assert classify_target("futures_fund/decision_qa.py") == "code"


def test_classify_target_and_can_autofix_agree():
    # protected => not autofixable; the other three classes => autofixable.
    for p in ("agents/flow_sentiment.md", "config.yaml", "futures_fund/reflect.py"):
        assert classify_target(p) != "protected"
        assert can_autofix(p) is True
    for p in ("futures_fund/risk_gate.py", "futures_fund/sentiment_audit.py"):
        assert classify_target(p) == "protected"
        assert can_autofix(p) is False


# --------------------------------------------------------------------------- #
# ImprovementProposal model                                                    #
# --------------------------------------------------------------------------- #


def test_improvement_proposal_validates():
    p = ImprovementProposal.model_validate(
        {
            "issue": "flow lane hallucinated citations 3 cycles running",
            "evidence": [
                {"cycle": 42, "agent": "flow", "item_id": "sol_news_77"},
                {"cycle": 43, "agent": "flow", "item_id": "fake_id_99"},
            ],
            "target_file": "agents/flow_sentiment.md",
            "classification": "prompt",
            "fix_summary": "tighten the cite-only-real-ids rule with a worked counter-example",
            "safe_to_autofix": True,
            "test_plan": "re-run tests/test_agent_examples.py; verify flow example still validates",
        }
    )
    assert p.classification == "prompt"
    assert p.safe_to_autofix is True
    assert len(p.evidence) == 2


def test_improvement_proposal_rejects_bad_classification():
    with pytest.raises(ValidationError):
        ImprovementProposal.model_validate(
            {
                "issue": "x",
                "evidence": [],
                "target_file": "config.yaml",
                "classification": "nonsense",
                "fix_summary": "y",
                "safe_to_autofix": False,
                "test_plan": "z",
            }
        )


def test_improvement_proposal_protected_is_not_safe_to_autofix_by_construction():
    # a protected target may exist in a proposal, but a well-formed OVERSEER never marks it safe.
    p = ImprovementProposal.model_validate(
        {
            "issue": "auditor advisory recurs",
            "evidence": [{"cycle": 7, "agent": "narrative"}],
            "target_file": "futures_fund/sentiment_audit.py",
            "classification": "protected",
            "fix_summary": "surface to human — never auto-applied",
            "safe_to_autofix": False,
            "test_plan": "human review",
        }
    )
    assert p.classification == "protected"
    assert p.safe_to_autofix is False
    # the gate must agree the target is not autofixable.
    assert can_autofix(p.target_file) is False


# --------------------------------------------------------------------------- #
# record_improvement — auditable, atomic, dual-format journal                  #
# --------------------------------------------------------------------------- #


def _entry(applied: bool, surfaced: bool) -> dict:
    return {
        "detected": "flow lane hallucinated 3 cycles running",
        "classification": "prompt",
        "target": "agents/flow_sentiment.md",
        "fix_summary": "tighten cite-only-real-ids rule",
        "applied": applied,
        "test_result": "778 passed",
        "surfaced": surfaced,
    }


def test_record_improvement_writes_both_files(tmp_path):
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    record_improvement(tmp_path, _entry(applied=True, surfaced=False), ts=ts)

    md = (tmp_path / "improvement-journal.md").read_text()
    assert "flow lane hallucinated 3 cycles running" in md
    assert "agents/flow_sentiment.md" in md
    assert "2026-06-15" in md  # timestamp came from the injected ts, not the clock

    jl = (tmp_path / "improvement-journal.jsonl").read_text()
    rows = [json.loads(x) for x in jl.splitlines() if x.strip()]
    assert len(rows) == 1
    assert rows[0]["target"] == "agents/flow_sentiment.md"
    assert rows[0]["applied"] is True
    assert rows[0]["surfaced"] is False
    assert rows[0]["ts"] == ts.isoformat()


def test_record_improvement_appends_not_overwrites(tmp_path):
    ts1 = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    ts2 = datetime(2026, 6, 15, 16, 0, tzinfo=UTC)
    record_improvement(tmp_path, _entry(applied=True, surfaced=False), ts=ts1)
    record_improvement(tmp_path, _entry(applied=False, surfaced=True), ts=ts2)

    rows = [
        json.loads(x)
        for x in (tmp_path / "improvement-journal.jsonl").read_text().splitlines()
        if x.strip()
    ]
    assert len(rows) == 2
    assert rows[0]["applied"] is True and rows[0]["surfaced"] is False
    assert rows[1]["applied"] is False and rows[1]["surfaced"] is True

    md = (tmp_path / "improvement-journal.md").read_text()
    assert md.count("flow lane hallucinated 3 cycles running") == 2


def test_record_improvement_atomic_no_tmp_left(tmp_path):
    ts = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    record_improvement(tmp_path, _entry(applied=True, surfaced=False), ts=ts)
    # the atomic writer must not leave a .tmp turd behind.
    assert not list(tmp_path.glob("*.tmp"))


def test_record_improvement_jsonl_self_heals_torn_line(tmp_path):
    # a previous crash left a trailing fragment with NO newline; the next append must not
    # concatenate onto it (mirrors the content_store / decision_qa_cli append pattern).
    p = tmp_path / "improvement-journal.jsonl"
    p.write_text('{"partial": "frag-no-newline"')
    record_improvement(
        tmp_path, _entry(applied=True, surfaced=False), ts=datetime(2026, 6, 15, tzinfo=UTC)
    )
    lines = p.read_text().splitlines()
    # the fragment is its own (garbage) line; our new record parses cleanly on the next line.
    assert len(lines) == 2
    assert json.loads(lines[-1])["target"] == "agents/flow_sentiment.md"


def test_record_improvement_tolerates_missing_optional_keys(tmp_path):
    # an entry missing some keys must still write (defaults), never raise.
    ts = datetime(2026, 6, 15, tzinfo=UTC)
    record_improvement(tmp_path, {"detected": "partial entry"}, ts=ts)
    rows = [
        json.loads(x)
        for x in (tmp_path / "improvement-journal.jsonl").read_text().splitlines()
        if x.strip()
    ]
    assert rows[0]["detected"] == "partial entry"
    assert rows[0]["applied"] is False  # default
    assert rows[0]["surfaced"] is False  # default
