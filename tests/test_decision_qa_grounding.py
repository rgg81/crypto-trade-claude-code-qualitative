"""Regression tests for decision_qa grounding fidelity (findings 5 + 6).

* Finding 5 — the coin-tag half of the hallucination check must mirror the Auditor's check 2
  (``claim_supports_coin``) which uses ALL semantics: a citation is hallucinated when ANY
  claimed coin is missing from the item's tags. QA previously used ANY semantics (clean if
  ANY claimed coin matched), so a lane could launder weak evidence for coin X by co-tagging a
  coin Y and citing only a Y-item. We pin QA to the SAME verdict the auditor returns.
* Finding 6 — a NONNEUTRAL (directional) read that cites NOTHING must not score perfect
  reliability. Asserting a strong stance on zero evidence is the opposite of grounded, and must
  be counted as an ungrounded unit so reliability does not seed at 1.0.

No network: tmp content store + crafted cycle artifacts. Time from artifacts.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.content_store import ContentItem, make_id, store_items
from futures_fund.contracts import AgentProposal, SentimentRead
from futures_fund.cycle_io import cycle_dir
from futures_fund.decision_qa import analyze_cycle, update_reliability
from futures_fund.sentiment_audit import review_cycle

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
AS_OF = NOW
PAST = NOW - timedelta(hours=6)


def _mk_item(*, source: str, url: str, title: str, coins: list[str]) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title), source=source, feed=f"https://{source}.example/rss",
        url=url, title=title, body="", coins=coins, published_ts=PAST, fetched_ts=PAST,
    )


def _seed_store(content_dir: Path) -> dict[str, str]:
    btc_a = _mk_item(source="coindesk", url="https://x.example/btc-a",
                     title="BTC ETF inflows", coins=["BTC"])
    btc_b = _mk_item(source="theblock", url="https://x.example/btc-b",
                     title="Whales accumulate BTC", coins=["BTC"])
    eth_a = _mk_item(source="decrypt", url="https://x.example/eth-a",
                     title="ETH staking surge", coins=["ETH"])
    store_items(str(content_dir), [btc_a, btc_b, eth_a])
    return {"btc_a": btc_a.id, "btc_b": btc_b.id, "eth_a": eth_a.id}


def _read(*, agent, coin, stance, level, s, confidence, claims, rationale="r"):
    return {
        "agent": agent, "coin": coin, "stance": stance, "level": level, "s": s,
        "confidence": confidence, "claims": claims, "rationale": rationale,
        "as_of_ts": AS_OF.isoformat(),
    }


def _claim(text, item_ids, coins):
    return {"text": text, "item_ids": item_ids, "coins": coins}


def _seed_cycle(state_dir: Path, cycle: int, *, reads=None) -> Path:
    cdir = cycle_dir(str(state_dir), cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    if reads is not None:
        (cdir / "sentiment_reads.json").write_text(json.dumps(reads))
    return cdir


# --------------------------------------------------------------------------- #
# Finding 5 — ALL-semantics coin-tag check, mirroring the auditor              #
# --------------------------------------------------------------------------- #


def test_multicoin_claim_missing_one_tag_is_hallucinated(tmp_path: Path) -> None:
    """claim coins=['BTC','ETH'] citing a BTC-ONLY item -> 1 hallucinated citation (ETH missing)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"]], ["BTC", "ETH"])]),
    ]
    _seed_cycle(state_dir, 1, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 1)
    assert qa.lanes["flow"].hallucinated_citations == 1
    assert qa.lanes["flow"].total_citations == 1


def test_qa_matches_auditor_check2_on_multicoin_claim(tmp_path: Path) -> None:
    """QA's hallucination count must equal the Auditor's check-2 verdict on the SAME read."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads_dicts = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"]], ["BTC", "ETH"])]),
    ]
    _seed_cycle(state_dir, 2, reads=reads_dicts)

    qa = analyze_cycle(str(state_dir), str(content_dir), 2)

    # the auditor on the SAME read flags claim_supports_coin (ETH not tagged). The finding is a
    # MISMATCH (not just an advisory) only when it lands on a TRADED coin, so we give the auditor a
    # BTC proposal — exactly the situation QA's hallucination count is meant to mirror. (We do NOT
    # weaken the auditor; we exercise it on a traded coin so its ALL-semantics check 2 fires fatal.)
    reads_models = [SentimentRead.model_validate(r) for r in reads_dicts]
    btc_proposal = AgentProposal(
        symbol="BTCUSDT", direction="long", entry=100.0, stop=95.0,
        take_profits=[110.0], atr=2.0, confidence=0.8,
    )
    verdict = review_cycle(str(content_dir), reads_models, [], [btc_proposal], now=NOW)
    assert "claim_supports_coin" in verdict.mismatches
    assert qa.lanes["flow"].hallucinated_citations == 1  # SAME verdict, ALL-semantics


def test_fully_tagged_multicoin_claim_is_clean(tmp_path: Path) -> None:
    """A claim coins=['BTC'] citing a BTC item stays clean (no regression on the happy path)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 3, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 3)
    assert qa.lanes["flow"].hallucinated_citations == 0
    assert qa.lanes["flow"].total_citations == 2


def test_multicoin_claim_on_multicoin_item_is_clean(tmp_path: Path) -> None:
    """When the item actually tags BOTH claimed coins, ALL-semantics passes it clean."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    multi = _mk_item(source="cd", url="https://x.example/multi", title="BTC and ETH roundup",
                     coins=["BTC", "ETH"])
    store_items(str(content_dir), [multi])
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [multi.id], ["BTC", "ETH"])]),
    ]
    _seed_cycle(state_dir, 4, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 4)
    assert qa.lanes["flow"].hallucinated_citations == 0


# --------------------------------------------------------------------------- #
# Finding 6 — directional read with zero citations does not score perfect      #
# --------------------------------------------------------------------------- #


def test_directional_zero_citation_read_is_penalised(tmp_path: Path) -> None:
    """A nonneutral read citing NOTHING must register an ungrounded unit (rate > 0)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.9, claims=[_claim("c", [], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 5, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 5)
    flow = qa.lanes["flow"]
    assert flow.n_nonneutral == 1
    # an unsupported directional read counts as an ungrounded unit, so the rate is non-zero.
    assert flow.hallucination_rate > 0.0


def test_directional_zero_citation_read_does_not_seed_reliability_one(tmp_path: Path) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.9, claims=[_claim("c", [], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 1, reads=reads)
    qa = analyze_cycle(str(state_dir), str(content_dir), 1)
    rel = update_reliability(str(memory_dir), qa, alpha=0.3)
    assert rel["flow"]["reliability"] < 1.0


def test_directional_zero_citation_no_claims_at_all_is_penalised(tmp_path: Path) -> None:
    """A directional read with an empty claims list (no citation occurrence) is also ungrounded."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    reads = [
        _read(agent="narrative", coin="BTC", stance="bearish", level="negative", s=-0.5,
              confidence=0.8, claims=[]),
    ]
    _seed_cycle(state_dir, 6, reads=reads)
    qa = analyze_cycle(str(state_dir), str(content_dir), 6)
    assert qa.lanes["narrative"].hallucination_rate > 0.0


def test_neutral_zero_citation_read_is_not_penalised(tmp_path: Path) -> None:
    """A NEUTRAL read citing nothing makes no claim of conviction -> not penalised (rate 0)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="neutral", level="neutral", s=0.0,
              confidence=0.5, claims=[_claim("c", [], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 7, reads=reads)
    qa = analyze_cycle(str(state_dir), str(content_dir), 7)
    assert qa.lanes["flow"].n_nonneutral == 0
    assert qa.lanes["flow"].hallucination_rate == 0.0


def test_grounded_directional_read_still_clean(tmp_path: Path) -> None:
    """A nonneutral read WITH a resolving, coin-matching citation stays clean (no false hit)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.9, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 8, reads=reads)
    qa = analyze_cycle(str(state_dir), str(content_dir), 8)
    assert qa.lanes["flow"].n_nonneutral == 1
    assert qa.lanes["flow"].hallucination_rate == 0.0


# --------------------------------------------------------------------------- #
# Finding 9 — update_reliability SKIPS a lane that produced no reads            #
# --------------------------------------------------------------------------- #


def test_absent_lane_is_not_credited_a_clean_cycle(tmp_path: Path) -> None:
    """A lane that produces NO reads in a cycle must not have a (fake) clean cycle blended into its
    reliability — n_cycles counts only cycles where the lane actually produced reads."""
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)
    # cycle 1: ONLY the flow lane produces a (clean) read.
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 1, reads=reads)
    qa = analyze_cycle(str(state_dir), str(content_dir), 1)
    rel = update_reliability(str(memory_dir), qa, alpha=0.3)

    # flow was observed: it completed one real cycle.
    assert rel["flow"]["n_cycles"] == 1
    # narrative / influencer produced NOTHING: they are present (atomic-write contract) but are
    # recorded as UNOBSERVED (n_cycles == 0) — never credited a clean cycle.
    for lane in ("narrative", "influencer"):
        assert lane in rel
        assert rel[lane]["n_cycles"] == 0
        assert set(rel[lane]) == {"reliability", "n_cycles",
                                  "hallucination_rate_ewma", "last_cycle"}


def test_absent_lane_first_real_cycle_seeds_not_blends(tmp_path: Path) -> None:
    """An unobserved lane (n_cycles==0) that LATER produces a hallucinating read SEEDS from that
    cycle's true (low) score — it does not blend against the fabricated clean prior, so its
    reliability reflects the bad read fully."""
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    _seed_store(content_dir)
    # cycle 1: only flow reads -> narrative seeded UNOBSERVED (n_cycles 0).
    _seed_cycle(state_dir, 1, reads=[
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", ["fake"], ["BTC"])]),
    ])
    update_reliability(str(memory_dir), analyze_cycle(str(state_dir), str(content_dir), 1),
                       alpha=0.3)
    # cycle 2: narrative's FIRST real read, and it fully hallucinates -> reliability seeds low.
    _seed_cycle(state_dir, 2, reads=[
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.9, claims=[_claim("c", ["fake-1", "fake-2"], ["BTC"])]),
    ])
    rel2 = update_reliability(str(memory_dir), analyze_cycle(str(state_dir), str(content_dir), 2),
                              alpha=0.3)
    assert rel2["narrative"]["n_cycles"] == 1            # first OBSERVED cycle
    assert rel2["narrative"]["hallucination_rate_ewma"] == 1.0  # SEED, not blended vs fake prior
    assert rel2["narrative"]["reliability"] < 0.5


def test_absent_lane_with_prior_is_carried_forward_unchanged(tmp_path: Path) -> None:
    """A lane with prior history that produces NO reads this cycle keeps its prior record EXACTLY
    (no blend, no n_cycles bump) — an absent cycle neither raises nor lowers its trust."""
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)
    # cycle 1: flow produces a clean read -> a real prior for flow.
    _seed_cycle(state_dir, 1, reads=[
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ])
    rel1 = update_reliability(str(memory_dir), analyze_cycle(str(state_dir), str(content_dir), 1),
                              alpha=0.3)
    flow_before = dict(rel1["flow"])
    # cycle 2: ONLY narrative reads; flow is absent and must be carried forward UNCHANGED.
    _seed_cycle(state_dir, 2, reads=[
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ])
    rel2 = update_reliability(str(memory_dir), analyze_cycle(str(state_dir), str(content_dir), 2),
                              alpha=0.3)
    assert rel2["flow"] == flow_before  # untouched: same reliability, n_cycles, last_cycle


# --------------------------------------------------------------------------- #
# Finding 10 — a corrupt prior reliability is clamped to [0, 1] before the EWMA #
# --------------------------------------------------------------------------- #


def test_corrupt_prior_reliability_is_clamped(tmp_path: Path) -> None:
    """A prior file with an out-of-range reliability (e.g. 9.0 from a corrupt write) must be clamped
    to [0, 1] before the blend, and the blended result must stay in [0, 1]."""
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)
    memory_dir.mkdir(parents=True, exist_ok=True)
    # a corrupt prior: reliability way out of range, with a real observed cycle so it blends.
    (memory_dir / "agent_reliability.json").write_text(json.dumps({
        "flow": {"reliability": 9.0, "n_cycles": 3,
                 "hallucination_rate_ewma": -4.0, "last_cycle": 0},
    }))
    _seed_cycle(state_dir, 1, reads=[
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ])
    rel = update_reliability(str(memory_dir), analyze_cycle(str(state_dir), str(content_dir), 1),
                             alpha=0.3)
    assert 0.0 <= rel["flow"]["reliability"] <= 1.0
    assert 0.0 <= rel["flow"]["hallucination_rate_ewma"] <= 1.0
