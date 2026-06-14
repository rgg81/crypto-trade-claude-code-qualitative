"""Offline conformance tests for the DECISION-side agent prompt example fixtures.

Each example in tests/fixtures/agent_examples/ is the canonical OUTPUT shape promised by an
agent's prompt (agents/*.md). These tests pin those examples to the pydantic contracts so a drift
between a prompt's documented JSON and the code that consumes it is caught at test time. NO network,
no clock, no exchange — pure model_validate over static JSON.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from futures_fund.contracts import (
    AgentProposal,
    SentimentPlan,
    rating_to_direction,
)
from futures_fund.lessons import Lesson
from futures_fund.models import CrowdMood

FIX = Path(__file__).parent / "fixtures" / "agent_examples"

# the five valid SentimentPlan tiers and the five valid crowd moods
RATINGS = {"strong_long", "long", "flat", "short", "strong_short"}
MOODS = {"euphoric", "greedy", "neutral", "fearful", "capitulation"}


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


# --------------------------------------------------------------------------- #
# Bull / Bear — {coin, thesis, key_points[], confidence}                       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", ["bull.json", "bear.json"])
def test_debate_example_shape(name: str):
    data = _load(name)
    assert isinstance(data["coin"], str) and data["coin"]
    assert isinstance(data["thesis"], str) and data["thesis"]
    assert isinstance(data["key_points"], list) and data["key_points"]
    assert all(isinstance(p, str) and p for p in data["key_points"])
    assert 0.0 <= float(data["confidence"]) <= 1.0


# --------------------------------------------------------------------------- #
# Decider — {plans[], proposals[], management[], triggers[], crowd_mood}        #
# --------------------------------------------------------------------------- #
def test_decider_plans_validate_against_sentiment_plan():
    data = _load("decider.json")
    plans = [SentimentPlan.model_validate(p) for p in data["plans"]]
    assert plans, "decider example must carry at least one plan"
    for p in plans:
        assert p.rating in RATINGS
        assert p.thesis and p.falsifiable_prediction
        assert 0.0 <= p.confidence <= 1.0
    # the ladder is exercised: at least one directional plan AND one earned flat
    assert any(rating_to_direction(p.rating) is not None for p in plans)
    assert any(p.rating == "flat" for p in plans)


def test_decider_proposals_validate_against_agent_proposal():
    data = _load("decider.json")
    proposals = [AgentProposal.model_validate(p) for p in data["proposals"]]
    assert proposals, "decider example must carry at least one proposal"
    for ap in proposals:
        assert ap.direction in {"long", "short"}
        assert ap.take_profits, "every proposal needs at least one take-profit"
        assert ap.atr > 0
        assert 0.0 <= ap.confidence <= 1.0
        assert 0.0 < ap.risk_mult <= 1.0


def test_decider_nearest_tp_is_at_least_2R():
    """The prompt promises the nearest take-profit is >= 2R; pin it so a thin example can't slip."""
    data = _load("decider.json")
    for raw in data["proposals"]:
        ap = AgentProposal.model_validate(raw)
        risk = abs(ap.entry - ap.stop)
        assert risk > 0
        if ap.direction == "long":
            assert ap.stop < ap.entry, "a long's stop sits below entry"
            nearest_tp = min(t for t in ap.take_profits if t > ap.entry)
            reward = nearest_tp - ap.entry
        else:
            assert ap.stop > ap.entry, "a short's stop sits above entry"
            nearest_tp = max(t for t in ap.take_profits if t < ap.entry)
            reward = ap.entry - nearest_tp
        assert reward >= 2.0 * risk, f"{ap.symbol} nearest TP must be >= 2R"


def test_decider_proposal_direction_matches_plan_rating():
    """A proposal only flows from a non-flat plan, and its direction matches the rating's side."""
    data = _load("decider.json")
    plan_dir = {
        p["symbol"]: rating_to_direction(p["rating"]) for p in data["plans"]
    }
    for raw in data["proposals"]:
        ap = AgentProposal.model_validate(raw)
        assert plan_dir.get(ap.symbol) == ap.direction
    # the FLAT plan emits no proposal
    flat_syms = {p["symbol"] for p in data["plans"] if p["rating"] == "flat"}
    proposed_syms = {p["symbol"] for p in data["proposals"]}
    assert not (flat_syms & proposed_syms), "a flat plan must not carry a proposal"


def test_decider_proposals_carry_contributing_agents():
    """LEARNING-LOOP: the gate journals each proposal's `contributing_agents` and metrics credits
    per-expert hit-rates from it. The decider's example MUST therefore carry a non-empty
    contributing_agents list on every proposal (AgentProposal allows extra fields — this is
    metadata on the dict, validated separately from the model)."""
    data = _load("decider.json")
    assert data["proposals"], "decider example must carry at least one proposal"
    for raw in data["proposals"]:
        agents = raw.get("contributing_agents")
        assert isinstance(agents, list) and agents, (
            f"{raw.get('symbol')} proposal must carry a non-empty contributing_agents list"
        )
        assert all(isinstance(a, str) and a for a in agents)
        # AgentProposal still validates (extra fields tolerated).
        AgentProposal.model_validate(raw)


def test_decider_crowd_mood_validates():
    data = _load("decider.json")
    cm = CrowdMood.model_validate(data["crowd_mood"])
    assert cm.mood in MOODS
    assert 0.0 <= cm.dispersion <= 1.0


def test_decider_management_and_triggers_are_lists():
    data = _load("decider.json")
    assert isinstance(data["management"], list)
    assert isinstance(data["triggers"], list)


# --------------------------------------------------------------------------- #
# Reflector — {lessons:[{text, polarity, regime, tags, importance, provenance}]} #
# --------------------------------------------------------------------------- #
def test_reflector_lessons_validate_against_lesson():
    data = _load("reflector.json")
    ts = datetime(2026, 6, 13, tzinfo=UTC)
    lessons = [Lesson.model_validate({**lz, "ts": ts}) for lz in data["lessons"]]
    assert lessons
    for lz in lessons:
        assert lz.text
        assert lz.polarity in {"restrictive", "enabling", "process"}
        assert 1 <= lz.importance <= 10
        assert lz.provenance, "every lesson cites its source decision id(s)"


def test_reflector_is_two_sided_with_an_enabling_lesson():
    """The prompt mandates >= 1 enabling lesson when winners/missed-ops exist; pin the example."""
    data = _load("reflector.json")
    polarities = {lz.get("polarity") for lz in data["lessons"]}
    assert "enabling" in polarities, "reflector example must mint at least one enabling lesson"
    # and it mines BOTH a long and a short enabling lesson (market-neutral on psychology)
    enabling_tags = {
        tag for lz in data["lessons"] if lz.get("polarity") == "enabling" for tag in lz["tags"]
    }
    assert {"long", "short"} <= enabling_tags, "two-sided: both a long AND a short enabling lesson"
