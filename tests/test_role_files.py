"""Role-file + orchestration-doc conformance harness for Operation ORACLE.

Forked from the base desk's test_role_files.py and adapted to the qualitative SENTIMENT funnel:

  * every ``agents/*.md`` REFERENCED by SKILL.md exists and carries its required sections
    (``## Mission`` always; ``## Output`` for the reasoning/decision agents);
  * every ``uv run python scripts/X.py`` command in SKILL.md resolves to a real file under
    ``scripts/`` (the orchestration doc cannot reference a script that does not exist);
  * MISSION.md is the ORACLE charter (names the operation, the 1% floor, sentiment direction).

Parsing SKILL.md directly (rather than hard-coding the role/script lists) keeps the doc and the
codebase locked together: rename or drop a script/agent and the doc-vs-disk check fails here.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

# Repo root = the parent of this tests/ dir, so the harness is cwd-independent.
ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "SKILL.md"

# The deterministic Auditor/gate/executor are CODE, not agent role files — the agent docs that must
# exist all carry an `## Output` JSON contract. (No agent on this desk is exempt from `## Output`:
# the base desk's risk_manager/portfolio_manager prose docs are dropped here — risk/exec is pure
# Python.) Every referenced agent therefore needs BOTH `## Mission` and `## Output`.
_REQUIRED_SECTIONS = ("## Mission", "## Output")

# The sentiment team we EXPECT the orchestration doc to dispatch. Asserting this explicit set (on
# top of the parsed-from-SKILL set) guards against SKILL.md silently dropping a funnel stage.
_EXPECTED_ROLES = {
    "content_summarizer",          # crawler loop (Sonnet)
    "flow_sentiment",              # decision loop expert (Sonnet)
    "narrative_sentiment",         # decision loop expert (Sonnet)
    "influencer_sentiment",        # decision loop expert (Sonnet)
    "bull", "bear",                # debate (Opus)
    "decider",                     # judge + plan/proposal author (Opus)
    "reflector",                   # post-trade learning (Opus)
}


def _skill_text() -> str:
    assert SKILL.exists(), f"missing orchestration doc: {SKILL}"
    return SKILL.read_text()


def _referenced_agents(text: str) -> list[str]:
    """Every ``agents/<role>.md`` path mentioned anywhere in SKILL.md, de-duped + sorted."""
    return sorted(set(re.findall(r"agents/([a-z_]+)\.md", text)))


def _referenced_scripts(text: str) -> list[str]:
    """Every ``uv run python scripts/<x>.py`` command in SKILL.md, de-duped + sorted."""
    return sorted(set(re.findall(r"uv run python scripts/([a-z_0-9]+)\.py", text)))


# --------------------------------------------------------------------------- agent role files


def test_skill_references_the_full_sentiment_team():
    """SKILL.md must dispatch the entire expected funnel — no stage silently dropped."""
    referenced = set(_referenced_agents(_skill_text()))
    missing = _EXPECTED_ROLES - referenced
    assert not missing, f"SKILL.md does not reference these expected agents: {sorted(missing)}"


@pytest.mark.parametrize("role", sorted(_EXPECTED_ROLES))
def test_referenced_role_file_exists_and_has_sections(role):
    """Every agent SKILL.md dispatches must exist and carry its required contract sections."""
    p = ROOT / "agents" / f"{role}.md"
    assert p.exists(), f"missing role file referenced by SKILL.md: {p}"
    text = p.read_text()
    for section in _REQUIRED_SECTIONS:
        assert section in text, f"{role}.md missing required section: {section!r}"


def test_every_agent_referenced_in_skill_resolves_on_disk():
    """No SKILL.md `agents/<x>.md` reference may dangle (catches a typo'd/renamed role file)."""
    for role in _referenced_agents(_skill_text()):
        p = ROOT / "agents" / f"{role}.md"
        assert p.exists(), f"SKILL.md references a non-existent role file: agents/{role}.md"


# --------------------------------------------------------------------------- script references


def test_skill_script_commands_all_resolve():
    """Every `uv run python scripts/X.py` in SKILL.md must point at a real file in scripts/."""
    scripts = _referenced_scripts(_skill_text())
    assert scripts, "SKILL.md references no scripts — the orchestration doc is empty?"
    for name in scripts:
        p = ROOT / "scripts" / f"{name}.py"
        assert p.exists(), f"SKILL.md references a non-existent script: scripts/{name}.py"


def test_skill_covers_both_loops_key_scripts():
    """Both loops' load-bearing CLIs must be named in SKILL.md (the doc-vs-funnel contract)."""
    scripts = set(_referenced_scripts(_skill_text()))
    crawler = {"crawl_due_check", "crawl_cli", "summarize_apply_cli"}
    decision = {"due_check", "universe_cli", "preflight", "evidence_cli",
                "retrieve_lessons_cli", "audit_cli", "gate_execute_cli",
                "reflect_cli", "record_lessons_cli", "self_audit_cli"}
    assert crawler <= scripts, f"SKILL.md crawler loop missing: {sorted(crawler - scripts)}"
    assert decision <= scripts, f"SKILL.md decision loop missing: {sorted(decision - scripts)}"


# --------------------------------------------------------------------------- the charter


def test_mission_file_exists_and_is_the_charter():
    """MISSION.md is the ORACLE charter: names the operation, the 1% floor, sentiment-direction."""
    t = (ROOT / "MISSION.md").read_text()
    assert "OPERATION ORACLE" in t
    assert "1%" in t                         # the monthly FLOOR to beat
    assert "SENTIMENT" in t.upper()          # direction is qualitative, not price


def test_hard_rule_and_readme_docs_exist():
    """The orchestration doc set is complete (skill + hard rules + charter + readme)."""
    for name in ("SKILL.md", "CLAUDE.md", "MISSION.md", "README.md"):
        assert (ROOT / name).exists(), f"missing orchestration doc: {name}"
