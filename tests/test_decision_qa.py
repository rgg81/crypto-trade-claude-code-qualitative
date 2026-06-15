"""Offline tests for futures_fund/decision_qa.py — the per-cycle DECISION-QA engine.

Every test runs with NO live network: a tmp content store seeded via ``content_store.store_items``
with crafted items, plus crafted ``sentiment_reads.json`` / ``plans.json`` / ``auditor.json`` under
state/cycle/<N>/. We assert the engine re-derives, from GROUND TRUTH:

* a read citing a NON-EXISTENT item_id is counted hallucinated;
* a read citing a REAL item tagged to a DIFFERENT coin is counted hallucinated;
* an s/level round-trip mismatch is flagged as a mislabel;
* a directional plan with no matching-stance grounded read is ungrounded;
* two lanes citing the SAME items show high (Jaccard ~1) redundancy;
* reliability EWMA DROPS for a hallucinating lane across cycles;
* a missing artifact yields a 0/None metric and never crashes (fail-soft).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.content_store import ContentItem, make_id, store_items
from futures_fund.cycle_io import cycle_dir
from futures_fund.decision_qa import analyze_cycle, update_reliability

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
AS_OF = NOW
PAST = NOW - timedelta(hours=6)


# --------------------------------------------------------------------------- #
# fixtures                                                                     #
# --------------------------------------------------------------------------- #


def _mk_item(*, source: str, url: str, title: str, coins: list[str]) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title),
        source=source,
        feed=f"https://{source}.example/rss",
        url=url,
        title=title,
        body="",
        coins=coins,
        published_ts=PAST,
        fetched_ts=PAST,
    )


def _seed_store(content_dir: Path) -> dict[str, str]:
    """Two BTC items (two sources) + one ETH item, so we can craft cross-coin mistags."""
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


def _seed_cycle(state_dir: Path, cycle: int, *, reads=None, plans=None, auditor=None) -> Path:
    cdir = cycle_dir(str(state_dir), cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    if reads is not None:
        (cdir / "sentiment_reads.json").write_text(json.dumps(reads))
    if plans is not None:
        (cdir / "plans.json").write_text(json.dumps(plans))
    if auditor is not None:
        (cdir / "auditor.json").write_text(json.dumps(auditor))
    return cdir


# --------------------------------------------------------------------------- #
# hallucination: non-existent id                                               #
# --------------------------------------------------------------------------- #


def test_nonexistent_item_id_counted_hallucinated(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8,
              claims=[_claim("fabricated", ["sha1-not-real"], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 1, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 1)
    flow = qa.lanes["flow"]
    assert flow.hallucinated_citations == 1
    assert flow.total_citations == 1
    assert flow.total_claims == 1
    assert flow.n_reads == 1
    assert flow.n_nonneutral == 1
    assert flow.hallucination_rate == 1.0


# --------------------------------------------------------------------------- #
# hallucination: real id tagged to a DIFFERENT coin                            #
# --------------------------------------------------------------------------- #


def test_real_item_wrong_coin_counted_hallucinated(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    # claim is about BTC but cites the ETH-only item -> hallucinated (item exists, wrong coin).
    reads = [
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.7,
              claims=[_claim("mistag", [ids["eth_a"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 2, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 2)
    narr = qa.lanes["narrative"]
    assert narr.hallucinated_citations == 1
    assert narr.total_citations == 1


def test_correct_coin_citation_not_hallucinated(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8,
              claims=[_claim("ok", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 3, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 3)
    assert qa.lanes["flow"].hallucinated_citations == 0
    assert qa.lanes["flow"].total_citations == 2


# --------------------------------------------------------------------------- #
# mislabel: s does not round-trip level                                        #
# --------------------------------------------------------------------------- #


def test_s_level_mismatch_flagged(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    # level "positive" but s=-1.0 buckets to very_negative -> mislabel.
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=-1.0,
              confidence=0.5, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
        # a clean read that round-trips, NOT flagged.
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.5, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 4, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 4)
    assert len(qa.mislabels) == 1
    assert "positive" in qa.mislabels[0]
    assert "flow" in qa.mislabels[0]


# --------------------------------------------------------------------------- #
# ungrounded plan: directional plan with no matching-stance read               #
# --------------------------------------------------------------------------- #


def test_directional_plan_without_matching_stance_is_ungrounded(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    # The only BTC read is BEARISH; a LONG plan has no matching bullish grounded read -> ungrounded.
    reads = [
        _read(agent="flow", coin="BTC", stance="bearish", level="negative", s=-0.5,
              confidence=0.7, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
    ]
    plans = [
        {"symbol": "BTCUSDT", "rating": "long", "confidence": 0.7,
         "thesis": "t", "falsifiable_prediction": "p"},
    ]
    _seed_cycle(state_dir, 5, reads=reads, plans=plans)

    qa = analyze_cycle(str(state_dir), str(content_dir), 5)
    assert len(qa.ungrounded_plans) == 1
    assert "BTCUSDT" in qa.ungrounded_plans[0]


def test_directional_plan_with_matching_stance_is_grounded(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.7, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
    ]
    plans = [
        {"symbol": "BTCUSDT", "rating": "long", "confidence": 0.7,
         "thesis": "t", "falsifiable_prediction": "p"},
        # a flat plan is never ungrounded.
        {"symbol": "ETHUSDT", "rating": "flat", "confidence": 0.5,
         "thesis": "t", "falsifiable_prediction": "p"},
    ]
    _seed_cycle(state_dir, 6, reads=reads, plans=plans)

    qa = analyze_cycle(str(state_dir), str(content_dir), 6)
    assert qa.ungrounded_plans == []


def test_plan_grounded_only_by_hallucinated_read_is_ungrounded(tmp_path: Path) -> None:
    """A bullish read whose citation does NOT resolve cannot ground a long plan (must rest on a
    citation that resolves AND tags the coin)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.7, claims=[_claim("c", ["fake-id"], ["BTC"])]),
    ]
    plans = [
        {"symbol": "BTCUSDT", "rating": "long", "confidence": 0.7,
         "thesis": "t", "falsifiable_prediction": "p"},
    ]
    _seed_cycle(state_dir, 7, reads=reads, plans=plans)

    qa = analyze_cycle(str(state_dir), str(content_dir), 7)
    assert len(qa.ungrounded_plans) == 1


# --------------------------------------------------------------------------- #
# lane redundancy: two lanes citing the same items show high overlap           #
# --------------------------------------------------------------------------- #


def test_lanes_citing_same_items_show_high_redundancy(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    same = [ids["btc_a"], ids["btc_b"]]
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.6, claims=[_claim("c", same, ["BTC"])]),
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.6, claims=[_claim("c", same, ["BTC"])]),
    ]
    _seed_cycle(state_dir, 8, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 8)
    # identical cited sets -> Jaccard 1.0 for BTC and mean 1.0.
    assert qa.lane_redundancy_by_coin["BTC"] == 1.0
    assert qa.lane_redundancy_mean == 1.0


def test_lanes_citing_disjoint_items_show_zero_redundancy(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.6, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.6, claims=[_claim("c", [ids["btc_b"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 9, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 9)
    assert qa.lane_redundancy_by_coin["BTC"] == 0.0
    assert qa.lane_redundancy_mean == 0.0


def test_single_lane_coin_has_no_redundancy_entry(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.6, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
    ]
    _seed_cycle(state_dir, 10, reads=reads)

    qa = analyze_cycle(str(state_dir), str(content_dir), 10)
    assert "BTC" not in qa.lane_redundancy_by_coin
    assert qa.lane_redundancy_mean == 0.0


# --------------------------------------------------------------------------- #
# auditor signals                                                              #
# --------------------------------------------------------------------------- #


def test_auditor_signals_copied(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    reads = [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.6, claims=[_claim("c", [ids["btc_a"]], ["BTC"])]),
    ]
    auditor = {
        "passed": False,
        "checks": [],
        "mismatches": ["claim_citations_exist", "evidence_grounding"],
        "blocked_proposals": ["ETHUSDT"],
        "advisories": ["[sentiment_range] ADA off", "[stance_consistency] SOL off",
                       "[sentiment_range] DOT off"],
    }
    _seed_cycle(state_dir, 11, reads=reads, auditor=auditor)

    qa = analyze_cycle(str(state_dir), str(content_dir), 11)
    assert qa.auditor_passed is False
    assert qa.n_advisories == 3
    assert qa.n_blocked_proposals == 1
    assert qa.mismatch_checks == {"claim_citations_exist": 1, "evidence_grounding": 1}
    assert qa.advisory_checks == {"sentiment_range": 2, "stance_consistency": 1}


# --------------------------------------------------------------------------- #
# fail-soft: missing artifacts never crash                                     #
# --------------------------------------------------------------------------- #


def test_missing_all_artifacts_is_fail_soft(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    # cycle dir does not even exist
    qa = analyze_cycle(str(state_dir), str(content_dir), 99)
    assert qa.cycle == 99
    assert qa.auditor_passed is None
    assert qa.n_advisories == 0
    assert qa.mislabels == []
    assert qa.ungrounded_plans == []
    assert all(qa.lanes[ln].n_reads == 0 for ln in qa.lanes)


def test_malformed_reads_is_fail_soft(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    cdir = cycle_dir(str(state_dir), 12)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "sentiment_reads.json").write_text("{ not json ::")
    qa = analyze_cycle(str(state_dir), str(content_dir), 12)
    assert qa.lanes["flow"].n_reads == 0  # nothing parsed, no crash


# --------------------------------------------------------------------------- #
# reliability EWMA drops for a hallucinating lane                              #
# --------------------------------------------------------------------------- #


def _clean_reads(ids) -> list[dict]:
    return [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, claims=[_claim("c", [ids["btc_a"], ids["btc_b"]], ["BTC"])]),
    ]


def _halluc_reads() -> list[dict]:
    return [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.9, claims=[_claim("c", ["fake-1", "fake-2"], ["BTC"])]),
    ]


def test_reliability_drops_for_hallucinating_lane(tmp_path: Path) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)

    # cycle 1: clean flow read -> seed high reliability.
    _seed_cycle(state_dir, 1, reads=_clean_reads(ids))
    qa1 = analyze_cycle(str(state_dir), str(content_dir), 1)
    rel1 = update_reliability(str(memory_dir), qa1, alpha=0.3)
    flow_rel_1 = rel1["flow"]["reliability"]
    assert flow_rel_1 > 0.9
    assert rel1["flow"]["n_cycles"] == 1
    assert rel1["flow"]["hallucination_rate_ewma"] == 0.0
    assert rel1["flow"]["last_cycle"] == 1

    # cycle 2: hallucinating flow read -> reliability EWMA must DROP.
    _seed_cycle(state_dir, 2, reads=_halluc_reads())
    qa2 = analyze_cycle(str(state_dir), str(content_dir), 2)
    rel2 = update_reliability(str(memory_dir), qa2, alpha=0.3)
    flow_rel_2 = rel2["flow"]["reliability"]
    assert flow_rel_2 < flow_rel_1
    assert rel2["flow"]["n_cycles"] == 2
    assert rel2["flow"]["hallucination_rate_ewma"] > 0.0
    assert rel2["flow"]["last_cycle"] == 2

    # cycle 3: another hallucinating cycle -> keeps dropping (monotone under repeated bad cycles).
    _seed_cycle(state_dir, 3, reads=_halluc_reads())
    qa3 = analyze_cycle(str(state_dir), str(content_dir), 3)
    rel3 = update_reliability(str(memory_dir), qa3, alpha=0.3)
    assert rel3["flow"]["reliability"] < flow_rel_2


def test_reliability_persisted_atomically(tmp_path: Path) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 1, reads=_clean_reads(ids))
    qa = analyze_cycle(str(state_dir), str(content_dir), 1)
    update_reliability(str(memory_dir), qa, alpha=0.3)

    path = memory_dir / "agent_reliability.json"
    assert path.exists()
    data = json.loads(path.read_text())
    for lane in ("flow", "narrative", "influencer"):
        assert lane in data
        assert set(data[lane]) == {"reliability", "n_cycles",
                                   "hallucination_rate_ewma", "last_cycle"}


def test_calibration_penalises_high_confidence_hallucination(tmp_path: Path) -> None:
    """A lane that stakes HIGH confidence on a hallucinated read scores lower than one that hedges
    the same hallucination at low confidence."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)

    # high-confidence hallucination
    _seed_cycle(state_dir, 1, reads=[
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.95, claims=[_claim("c", ["fake"], ["BTC"])]),
    ])
    qa_hi = analyze_cycle(str(state_dir), str(content_dir), 1)
    assert qa_hi.lanes["flow"].calibration_penalty > 0.0

    # low-confidence (<0.6) hallucination stakes no calibration penalty
    _seed_cycle(state_dir, 2, reads=[
        _read(agent="narrative", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.4, claims=[_claim("c", ["fake"], ["BTC"])]),
    ])
    qa_lo = analyze_cycle(str(state_dir), str(content_dir), 2)
    assert qa_lo.lanes["narrative"].calibration_penalty == 0.0
