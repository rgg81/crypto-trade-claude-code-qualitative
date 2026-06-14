"""Conformance tests for the SENTIMENT-side agent example fixtures (agents/*.md outputs).

Each prompt in ``agents/`` ships a worked example; the matching fixture under
``tests/fixtures/agent_examples/`` must VALIDATE against the contract the prompt promises so a
drift in either the prompt's example or the contract is caught here. The expert fixtures are
validated against :class:`SentimentRead`/:class:`Claim` AND against the same §7.1 / stance-sign
discipline the deterministic Auditor re-derives, so an example we'd ship to a model can never itself
be one the Auditor would veto. The content_summarizer fixture validates against its informal
contract (the five ordinal levels + the <=2-sentence summary rule).
"""
import json
from pathlib import Path

import pytest

from futures_fund.contracts import SentimentRead
from futures_fund.sentiment_decay import LEVEL_TO_S, SentimentLevel, level_to_s, s_to_level

FIX = Path(__file__).parent / "fixtures" / "agent_examples"

# the three expert prompts -> the agent literal their SentimentReads must carry
EXPERT_FILES = {
    "flow_sentiment.json": "flow",
    "narrative_sentiment.json": "narrative",
    "influencer_sentiment.json": "influencer",
}

_LEVELS = set(LEVEL_TO_S)  # the five ordinal SentimentLevel values


def _load(name: str) -> list[dict]:
    return json.loads((FIX / name).read_text())


@pytest.mark.parametrize("name,agent", sorted(EXPERT_FILES.items()))
def test_expert_example_conforms_to_sentiment_read(name, agent):
    rows = _load(name)
    assert isinstance(rows, list) and rows, f"{name} must be a non-empty JSON list"
    for row in rows:
        read = SentimentRead.model_validate(row)  # strict (extra=forbid) contract validation
        assert read.agent == agent, f"{name} must carry agent={agent!r}"


@pytest.mark.parametrize("name", sorted(EXPERT_FILES))
def test_expert_example_s_round_trips_level(name):
    # the Auditor's sentiment_range check: s must re-bucket to the stated level (§7.1).
    for row in _load(name):
        read = SentimentRead.model_validate(row)
        assert read.s == level_to_s(read.level), f"{name}: s must equal the level's §7.1 anchor"
        assert s_to_level(read.s) == read.level, f"{name}: s must round-trip its ordinal level"


@pytest.mark.parametrize("name", sorted(EXPERT_FILES))
def test_expert_example_stance_agrees_with_sign(name):
    # stance must agree in sign with s: bullish>0, bearish<0, neutral==0.
    for row in _load(name):
        read = SentimentRead.model_validate(row)
        if read.stance == "bullish":
            assert read.s > 0, f"{name}: bullish stance needs s>0"
        elif read.stance == "bearish":
            assert read.s < 0, f"{name}: bearish stance needs s<0"
        else:
            assert read.s == 0, f"{name}: neutral stance needs s==0"


@pytest.mark.parametrize("name", sorted(EXPERT_FILES))
def test_expert_example_claims_cite_item_ids(name):
    # every claim must carry at least one item_id (the prompts forbid uncited directional claims),
    # and a high-conviction non-neutral read must span >=2 items across (implicitly) the evidence.
    for row in _load(name):
        read = SentimentRead.model_validate(row)
        for claim in read.claims:
            assert claim.item_ids, f"{name}: every claim must cite >=1 item_id"
        if read.stance != "neutral" and read.confidence >= 0.6:
            distinct = {iid for c in read.claims for iid in c.item_ids}
            assert len(distinct) >= 2, (
                f"{name}: high-conviction directional read must cite >=2 distinct item_ids"
            )


def test_content_summarizer_example_conforms():
    rows = _load("content_summarizer.json")
    assert isinstance(rows, list) and rows, "content_summarizer.json must be a non-empty JSON list"
    for row in rows:
        assert set(row) == {"item_id", "summary", "item_sentiment"}, f"unexpected keys: {set(row)}"
        assert isinstance(row["item_id"], str) and row["item_id"], "item_id must be a non-empty str"
        level: SentimentLevel = row["item_sentiment"]
        assert level in _LEVELS, f"item_sentiment must be one of {_LEVELS}, got {level!r}"
        # summary is at most two sentences (terse, operational).
        n_sentences = sum(row["summary"].count(p) for p in (".", "!", "?"))
        assert 0 < n_sentences <= 2, f"summary must be 1-2 sentences, got {n_sentences}"
