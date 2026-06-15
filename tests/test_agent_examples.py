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
PROMPTS = Path(__file__).resolve().parent.parent / "agents"

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


# item_id source-token convention used across the example fixtures: the slug after the coin prefix
# names the originating feed. e.g. sol_news_77 -> rss/news, sol_reddit_12 -> reddit, sol_st_05 ->
# stocktwits. The FLOW lane must count DISTINCT sources (not distinct items) before claiming
# confidence >= 0.6 — N items under one source value is ONE source, never multi-source confluence.
_ID_TOKEN_TO_SOURCE = {
    "news": "rss",
    "rss": "rss",
    "st": "stocktwits",
    "reddit": "reddit",
    "nitter": "twitter",
    "tg": "telegram",
    "telegram": "telegram",
    "forum": "forums",
    "forums": "forums",
}


def _source_of(item_id: str) -> str:
    # split coin_<source>_<n>; the middle token(s) name the feed.
    parts = item_id.split("_")
    for tok in parts[1:]:
        if tok in _ID_TOKEN_TO_SOURCE:
            return _ID_TOKEN_TO_SOURCE[tok]
    # fall back to the middle token so two different unknown feeds still count as distinct.
    return parts[1] if len(parts) > 1 else item_id


def test_flow_example_high_conf_read_spans_two_distinct_sources():
    # The cy12 BTC/flow failure: confidence 0.72 on 6 items / 1 source, rationale falsely claiming
    # "multi-source". The shipped flow example must itself satisfy the source-counting rule the
    # prompt now mandates — a >=0.6 directional read's cited ids must resolve to >=2 DISTINCT
    # sources, not merely >=2 item_ids under one feed.
    for row in _load("flow_sentiment.json"):
        read = SentimentRead.model_validate(row)
        if read.stance != "neutral" and read.confidence >= 0.6:
            distinct_ids = {iid for c in read.claims for iid in c.item_ids}
            distinct_sources = {_source_of(iid) for iid in distinct_ids}
            assert len(distinct_sources) >= 2, (
                "flow_sentiment.json: a confidence>=0.6 directional read must cite ids resolving "
                f"to >=2 distinct sources, got {distinct_sources} from {distinct_ids}"
            )


def test_flow_prompt_mandates_operational_source_counting():
    # The prose >=2-source rule was present but NOT being applied (cy8/cy9/cy12 single-source
    # high-conf reads). The prompt must carry an OPERATIONAL, executable checklist: literally COUNT
    # the distinct `source` values of the exact cited ids, cap at 0.5 when < 2, and forbid asserting
    # "multi-source" unless the citations back it — plus a worked single-source counter-example.
    text = (PROMPTS / "flow_sentiment.md").read_text().lower()
    # (1) an explicit instruction to COUNT the distinct source values of the cited ids
    assert "count" in text and "source" in text
    assert "distinct source" in text or "distinct `source`" in text
    # (2) the < 2-source cap rule, naming the 0.5 ceiling
    assert "0.5" in text
    assert "cap" in text or "neutral" in text
    # (3) the rationale-claim discipline: cannot assert multi-source unless ids back it
    assert "multi-source" in text or "multi source" in text
    # (4) a worked single-source counter-example mirroring the cy12 BTC failure (6 items, 1 source)
    assert "stocktwits" in text
    assert "one source" in text or "1 source" in text or "single source" in text


def test_narrative_example_high_conf_read_spans_two_distinct_sources():
    # The cy11 XRP/narrative failure: confidence 0.72 on 3 items / 1 source (all source=rss),
    # rationale falsely claiming the >=2-items / >=2-sources threshold was satisfied because the
    # lane mistook several distinct RSS articles for several distinct sources. The shipped narrative
    # example must itself satisfy the source-counting rule the prompt now mandates — a >=0.6
    # directional read's cited ids must resolve to >=2 DISTINCT sources (the item `source` field),
    # not merely >=2 item_ids that happen to all be RSS.
    for row in _load("narrative_sentiment.json"):
        read = SentimentRead.model_validate(row)
        if read.stance != "neutral" and read.confidence >= 0.6:
            distinct_ids = {iid for c in read.claims for iid in c.item_ids}
            distinct_sources = {_source_of(iid) for iid in distinct_ids}
            assert len(distinct_sources) >= 2, (
                "narrative_sentiment.json: a confidence>=0.6 directional read must cite ids "
                f"resolving to >=2 distinct sources, got {distinct_sources} from {distinct_ids}"
            )


def test_narrative_prompt_mandates_operational_source_counting():
    # The cy11 XRP failure: narrative cited 3 distinct RSS items as if they were "three distinct
    # sources" and stamped 0.72 — but all carry source=rss, which the Auditor counts as ONE source.
    # The prompt must carry an OPERATIONAL, executable checklist mirroring the flow fix: state that
    # several RSS articles/feeds all carry source=rss and are ONE source; literally COUNT the
    # distinct `source` values of the exact cited ids; cap at 0.5 when < 2; forbid asserting the
    # >=2-source threshold is satisfied unless the citations back it — plus a worked RSS-only
    # counter-example (3 RSS items = 1 source -> 0.5 cap) directly correcting the cy11 XRP misread.
    text = (PROMPTS / "narrative_sentiment.md").read_text().lower()
    # (1) an explicit instruction to COUNT the distinct source values of the cited ids
    assert "count" in text and "source" in text
    assert "distinct source" in text or "distinct `source`" in text
    # (2) the < 2-source cap rule, naming the 0.5 ceiling
    assert "0.5" in text
    assert "cap" in text or "neutral" in text
    # (3) the RSS-are-one-source statement: several RSS articles/feeds all = source=rss = ONE source
    assert "rss" in text
    assert "one source" in text or "1 source" in text or "single source" in text
    # (4) a worked RSS-only counter-example mirroring the cy11 XRP failure (3 RSS items, 1 source)
    assert "3 rss" in text or "three rss" in text


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
