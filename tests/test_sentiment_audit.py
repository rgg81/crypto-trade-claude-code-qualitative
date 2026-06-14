from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from futures_fund.content_store import ContentItem, make_id, store_items
from futures_fund.contracts import (
    AgentProposal,
    Claim,
    ResearchPlan,
    SentimentRead,
)
from futures_fund.sentiment_audit import (
    AuditCheck,
    AuditVerdict,
    audit_gate_ok,
    review_cycle,
)

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
AS_OF = NOW
PAST = NOW - timedelta(hours=6)  # published before the decision anchor (PIT-clean)
FUTURE = NOW + timedelta(hours=1)  # post-decision leakage


# --------------------------------------------------------------------------- #
# fixtures: a content store with BTC, ETH and AVAX items from distinct sources #
# --------------------------------------------------------------------------- #


def _mk_item(
    *,
    source: str,
    url: str,
    title: str,
    coins: list[str],
    published_ts: datetime = PAST,
) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title),
        source=source,
        feed=f"https://{source}.example/rss",
        url=url,
        title=title,
        body="",
        coins=coins,
        published_ts=published_ts,
        fetched_ts=published_ts,
    )


@pytest.fixture
def store(tmp_path: Path):
    """A content dir seeded with crafted items; returns (content_dir, ids dict)."""
    content_dir = tmp_path / "content"
    btc_a = _mk_item(
        source="coindesk", url="https://coindesk.example/btc-a",
        title="BTC ETF inflows accelerate", coins=["BTC"],
    )
    btc_b = _mk_item(
        source="theblock", url="https://theblock.example/btc-b",
        title="Whales accumulate BTC", coins=["BTC"],
    )
    btc_future = _mk_item(
        source="coindesk", url="https://coindesk.example/btc-future",
        title="BTC late news", coins=["BTC"], published_ts=FUTURE,
    )
    eth_a = _mk_item(
        source="coindesk", url="https://coindesk.example/eth-a",
        title="ETH staking surge", coins=["ETH"],
    )
    eth_b = _mk_item(
        source="theblock", url="https://theblock.example/eth-b",
        title="ETH L2 fees collapse", coins=["ETH"],
    )
    eth_future = _mk_item(
        source="coindesk", url="https://coindesk.example/eth-future",
        title="ETH late news", coins=["ETH"], published_ts=FUTURE,
    )
    avax_a = _mk_item(
        source="coindesk", url="https://coindesk.example/avax-a",
        title="AVAX subnet launch", coins=["AVAX"],
    )
    avax_future = _mk_item(
        source="coindesk", url="https://coindesk.example/avax-future",
        title="AVAX late news", coins=["AVAX"], published_ts=FUTURE,
    )
    degraded = _mk_item(
        source="randomblog", url="https://randomblog.example/btc-x",
        title="BTC to the moon", coins=["BTC"],
    )
    degraded_eth = _mk_item(
        source="randomblog", url="https://randomblog.example/eth-x",
        title="ETH to the moon", coins=["ETH"],
    )
    store_items(
        content_dir,
        [btc_a, btc_b, btc_future, eth_a, eth_b, eth_future,
         avax_a, avax_future, degraded, degraded_eth],
    )
    ids = {
        "btc_a": btc_a.id,
        "btc_b": btc_b.id,
        "btc_future": btc_future.id,
        "eth_a": eth_a.id,
        "eth_b": eth_b.id,
        "eth_future": eth_future.id,
        "avax_a": avax_a.id,
        "avax_future": avax_future.id,
        "degraded": degraded.id,
        "degraded_eth": degraded_eth.id,
    }
    return content_dir, ids


# --------------------------------------------------------------------------- #
# read / plan / proposal builders                                              #
# --------------------------------------------------------------------------- #


def _btc_read(ids: dict) -> SentimentRead:
    """A high-conviction bullish BTC read citing two distinct items across two sources."""
    return SentimentRead(
        agent="flow",
        coin="BTC",
        stance="bullish",
        level="positive",
        s=0.5,
        confidence=0.8,
        claims=[
            Claim(text="ETF inflows", item_ids=[ids["btc_a"]], coins=["BTC"]),
            Claim(text="whale accumulation", item_ids=[ids["btc_b"]], coins=["BTC"]),
        ],
        rationale="Strong on-chain accumulation and ETF demand point bullish.",
        as_of_ts=AS_OF,
    )


def _eth_read(ids: dict) -> SentimentRead:
    """A high-conviction bullish ETH read citing two distinct items across two sources."""
    return SentimentRead(
        agent="narrative",
        coin="ETH",
        stance="bullish",
        level="positive",
        s=0.5,
        confidence=0.8,
        claims=[
            Claim(text="staking surge", item_ids=[ids["eth_a"]], coins=["ETH"]),
            Claim(text="L2 fees collapse", item_ids=[ids["eth_b"]], coins=["ETH"]),
        ],
        rationale="Staking demand and cheaper L2 fees skew the narrative bullish.",
        as_of_ts=AS_OF,
    )


def _avax_read(ids: dict) -> SentimentRead:
    """A bullish AVAX read for a coin the desk does NOT trade (no proposal)."""
    return SentimentRead(
        agent="narrative",
        coin="AVAX",
        stance="bullish",
        level="positive",
        s=0.5,
        confidence=0.55,
        claims=[
            Claim(text="subnet launch", item_ids=[ids["avax_a"]], coins=["AVAX"]),
        ],
        rationale="Subnet launch draws constructive attention.",
        as_of_ts=AS_OF,
    )


def _btc_plan() -> ResearchPlan:
    return ResearchPlan(
        symbol="BTC",
        rating="long",
        confidence=0.8,
        thesis="Net-positive flow and bullish narrative justify a long.",
        falsifiable_prediction="BTC sentiment stays net-positive over the next 24h.",
    )


def _eth_plan() -> ResearchPlan:
    return ResearchPlan(
        symbol="ETH",
        rating="long",
        confidence=0.8,
        thesis="Net-positive ETH narrative justifies a long.",
        falsifiable_prediction="ETH sentiment stays net-positive over the next 24h.",
    )


def _btc_proposal() -> AgentProposal:
    return AgentProposal(
        symbol="BTCUSDT",
        direction="long",
        entry=65000.0,
        stop=63000.0,
        take_profits=[68000.0],
        atr=1500.0,
        confidence=0.8,
        rationale="Direction driven by bullish flow + narrative sentiment.",
        falsifiable_prediction="Sentiment remains net-positive.",
    )


def _eth_proposal() -> AgentProposal:
    return AgentProposal(
        symbol="ETHUSDT",
        direction="long",
        entry=3500.0,
        stop=3300.0,
        take_profits=[3900.0],
        atr=80.0,
        confidence=0.8,
        rationale="Direction driven by bullish ETH narrative.",
        falsifiable_prediction="ETH sentiment remains net-positive.",
    )


def _multi(ids: dict) -> dict:
    """A fully-valid two-proposal cycle: BTC + ETH each backed by clean reads/plans/proposals."""
    return {
        "reads": [_btc_read(ids), _eth_read(ids)],
        "plans": [_btc_plan(), _eth_plan()],
        "proposals": [_btc_proposal(), _eth_proposal()],
    }


def _run(store, **overrides) -> AuditVerdict:
    content_dir, ids = store
    reads = overrides.pop("reads", [_btc_read(ids)])
    plans = overrides.pop("plans", [_btc_plan()])
    proposals = overrides.pop("proposals", [_btc_proposal()])
    degraded = overrides.pop("degraded_sources", None)
    return review_cycle(
        content_dir,
        reads=reads,
        plans=plans,
        proposals=proposals,
        now=NOW,
        degraded_sources=degraded,
        **overrides,
    )


def _failed(verdict: AuditVerdict) -> set[str]:
    return {c.name for c in verdict.checks if not c.passed}


# --------------------------------------------------------------------------- #
# the fully-valid multi-proposal cycle PASSES with no blocks                   #
# --------------------------------------------------------------------------- #


def test_valid_multi_proposal_cycle_passes(store) -> None:
    _content, ids = store
    verdict = _run(store, **_multi(ids))
    assert verdict.passed is True, verdict.mismatches
    assert verdict.mismatches == []
    assert verdict.blocked_proposals == []
    assert verdict.advisories == []
    assert len(verdict.checks) == 9
    assert all(isinstance(c, AuditCheck) for c in verdict.checks)


def test_passed_means_no_fatal(store) -> None:
    _content, ids = store
    verdict = _run(store, **_multi(ids))
    # passed == no fatal findings; a clean cycle has none.
    assert verdict.passed is True
    assert verdict.mismatches == [c.name for c in verdict.checks if not c.passed]


# --------------------------------------------------------------------------- #
# FATAL checks: violation on a TRADED coin vetoes the cycle; the SAME           #
# violation on a NON-traded coin is ADVISORY ONLY (the AVAX-style case).        #
# --------------------------------------------------------------------------- #


def test_hallucinated_item_id_on_traded_coin_is_fatal(store) -> None:
    _content, ids = store
    bad = _btc_read(ids)
    bad.claims[0].item_ids = ["deadbeefdeadbeefdeadbeef0000000000000000"]
    verdict = _run(store, reads=[bad, _eth_read(ids)], **{
        "plans": [_btc_plan(), _eth_plan()],
        "proposals": [_btc_proposal(), _eth_proposal()],
    })
    assert verdict.passed is False
    assert "claim_citations_exist" in verdict.mismatches


def test_hallucinated_item_id_on_non_traded_coin_is_advisory(store) -> None:
    _content, ids = store
    # AVAX read cites a hallucinated id, but the desk does NOT trade AVAX (no AVAX proposal).
    avax = _avax_read(ids)
    avax.claims[0].item_ids = ["deadbeefdeadbeefdeadbeef0000000000000000"]
    verdict = _run(
        store,
        reads=[_btc_read(ids), avax],
        plans=[_btc_plan()],
        proposals=[_btc_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches
    assert "claim_citations_exist" not in verdict.mismatches
    assert any("AVAX" in a for a in verdict.advisories)
    # the bad AVAX read does NOT block the (traded) BTC proposal.
    assert verdict.blocked_proposals == []


def test_wrong_coin_citation_on_traded_coin_is_fatal(store) -> None:
    _content, ids = store
    bad = _btc_read(ids)
    bad.claims[0].item_ids = [ids["eth_a"]]  # ETH-only item under a BTC claim
    verdict = _run(store, reads=[bad])
    assert verdict.passed is False
    assert "claim_supports_coin" in verdict.mismatches
    assert "claim_citations_exist" not in verdict.mismatches


def test_wrong_coin_citation_on_non_traded_coin_is_advisory(store) -> None:
    _content, ids = store
    avax = _avax_read(ids)
    avax.claims[0].item_ids = [ids["eth_a"]]  # ETH item under an AVAX claim
    verdict = _run(
        store,
        reads=[_btc_read(ids), avax],
        plans=[_btc_plan()],
        proposals=[_btc_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches
    assert "claim_supports_coin" not in verdict.mismatches
    assert any("AVAX" in a for a in verdict.advisories)


def test_future_dated_item_on_traded_coin_is_fatal(store) -> None:
    _content, ids = store
    bad = _btc_read(ids)
    bad.claims[0].item_ids = [ids["btc_future"]]
    verdict = _run(store, reads=[bad])
    assert verdict.passed is False
    assert "point_in_time" in verdict.mismatches


def test_future_dated_item_on_non_traded_coin_is_advisory(store) -> None:
    _content, ids = store
    avax = _avax_read(ids)
    avax.claims[0].item_ids = [ids["avax_future"]]
    verdict = _run(
        store,
        reads=[_btc_read(ids), avax],
        plans=[_btc_plan()],
        proposals=[_btc_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches
    assert "point_in_time" not in verdict.mismatches
    assert any("AVAX" in a for a in verdict.advisories)


def test_price_leak_on_traded_proposal_is_fatal(store) -> None:
    _content, ids = store
    leaky = _btc_read(ids)
    leaky.rationale = "Long because price bounced off the 200-day moving average support."
    verdict = _run(store, reads=[leaky])
    assert verdict.passed is False
    assert "no_directional_price_leak" in verdict.mismatches


def test_price_leak_on_non_traded_coin_is_advisory(store) -> None:
    _content, ids = store
    # AVAX read carries price/TA language, but there is no AVAX proposal to protect.
    avax = _avax_read(ids)
    avax.rationale = "Long because price bounced off the 200-day moving average support."
    verdict = _run(
        store,
        reads=[_btc_read(ids), avax],
        plans=[_btc_plan()],
        proposals=[_btc_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches
    assert "no_directional_price_leak" not in verdict.mismatches
    assert any("AVAX" in a for a in verdict.advisories)


def test_ungrounded_traded_proposal_is_fatal(store) -> None:
    content_dir, ids = store
    # a BTC proposal with ZERO supporting BTC reads (only an AVAX read exists) -> fatal grounding.
    verdict = review_cycle(
        content_dir,
        reads=[_avax_read(ids)],
        plans=[_btc_plan()],
        proposals=[_btc_proposal()],
        now=NOW,
    )
    assert verdict.passed is False
    assert "evidence_grounding" in verdict.mismatches


def test_ungrounded_non_traded_coin_is_advisory_only(store) -> None:
    content_dir, ids = store
    # A directional DOGE plan that is ungrounded, but DOGE is NOT a traded coin (no proposal):
    # grounding is advisory for it, not fatal. BTC (traded) is grounded.
    plan = ResearchPlan(
        symbol="DOGE", rating="long", confidence=0.9,
        thesis="DOGE up.", falsifiable_prediction="DOGE up.",
    )
    verdict = review_cycle(
        content_dir,
        reads=[_btc_read(ids)],
        plans=[_btc_plan(), plan],
        proposals=[_btc_proposal()],
        now=NOW,
    )
    assert verdict.passed is True, verdict.mismatches
    assert "evidence_grounding" not in verdict.mismatches
    assert any("DOGE" in a for a in verdict.advisories)


def test_grounded_traded_proposal_passes_grounding(store) -> None:
    _content, ids = store
    verdict = _run(store, **_multi(ids))
    assert "evidence_grounding" not in verdict.mismatches


def test_flat_rating_for_non_traded_coin_is_not_a_violation(store) -> None:
    content_dir, ids = store
    plan = ResearchPlan(
        symbol="DOGE", rating="flat", confidence=0.5,
        thesis="No edge.", falsifiable_prediction="DOGE flat.",
    )
    verdict = review_cycle(
        content_dir,
        reads=[_btc_read(ids)],
        plans=[_btc_plan(), plan],
        proposals=[_btc_proposal()],
        now=NOW,
    )
    assert verdict.passed is True, verdict.mismatches
    assert "evidence_grounding" not in verdict.mismatches
    # a flat (directionless) plan produces no advisory either.
    assert not any("DOGE" in a for a in verdict.advisories)


# --------------------------------------------------------------------------- #
# PROPOSAL-BLOCKING checks: traded proposal -> passed True (no fatal) + symbol  #
# added to blocked_proposals so the gate skips it.                             #
# --------------------------------------------------------------------------- #


def test_thin_evidence_blocks_only_that_proposal(store) -> None:
    _content, ids = store
    # ETH read is single-source/single-item over-conviction; BTC is clean.
    thin_eth = _eth_read(ids)
    thin_eth.claims = [Claim(text="lone", item_ids=[ids["eth_a"]], coins=["ETH"])]
    verdict = _run(
        store,
        reads=[_btc_read(ids), thin_eth],
        plans=[_btc_plan(), _eth_plan()],
        proposals=[_btc_proposal(), _eth_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches  # blocking is NOT fatal
    assert "evidence_sufficiency" in verdict.mismatches
    assert "ETHUSDT" in verdict.blocked_proposals
    assert "BTCUSDT" not in verdict.blocked_proposals


def test_stance_inversion_blocks_only_that_proposal(store) -> None:
    _content, ids = store
    # ETH experts net-bearish but the ETH plan/proposal goes long -> inversion -> block ETH only.
    bearish_eth = _eth_read(ids)
    bearish_eth.stance = "bearish"
    bearish_eth.level = "very_negative"
    bearish_eth.s = -1.0
    eth_plan = _eth_plan()
    eth_plan.rating = "strong_long"
    verdict = _run(
        store,
        reads=[_btc_read(ids), bearish_eth],
        plans=[_btc_plan(), eth_plan],
        proposals=[_btc_proposal(), _eth_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches
    assert "stance_consistency" in verdict.mismatches
    assert "ETHUSDT" in verdict.blocked_proposals
    assert "BTCUSDT" not in verdict.blocked_proposals


def test_degraded_dominance_blocks_only_that_proposal(store) -> None:
    _content, ids = store
    # ETH read rests entirely on the degraded 'randomblog' source -> block ETH; BTC clean.
    deg_eth = SentimentRead(
        agent="influencer", coin="ETH", stance="bullish", level="positive",
        s=0.5, confidence=0.9,
        claims=[
            Claim(text="moon", item_ids=[ids["degraded_eth"]], coins=["ETH"]),
            Claim(text="moon2", item_ids=[ids["degraded_eth"]], coins=["ETH"]),
        ],
        rationale="Bullish chatter on social.", as_of_ts=AS_OF,
    )
    verdict = _run(
        store,
        reads=[_btc_read(ids), deg_eth],
        plans=[_btc_plan(), _eth_plan()],
        proposals=[_btc_proposal(), _eth_proposal()],
        degraded_sources={"randomblog"},
    )
    assert verdict.passed is True, verdict.mismatches
    assert "degraded_source_dominance" in verdict.mismatches
    assert "ETHUSDT" in verdict.blocked_proposals
    assert "BTCUSDT" not in verdict.blocked_proposals


def test_sentiment_range_on_sole_read_blocks_that_proposal(store) -> None:
    _content, ids = store
    # ETH's only supporting read has an s/level mismatch -> it is effectively ungrounded ->
    # the ETH proposal is BLOCKED (not fatal). BTC stays clean.
    broken_eth = _eth_read(ids)
    broken_eth.s = -1.0  # level says positive but s buckets very_negative
    verdict = _run(
        store,
        reads=[_btc_read(ids), broken_eth],
        plans=[_btc_plan(), _eth_plan()],
        proposals=[_btc_proposal(), _eth_proposal()],
    )
    assert verdict.passed is True, verdict.mismatches  # not fatal
    assert "sentiment_range" in verdict.mismatches
    assert "ETHUSDT" in verdict.blocked_proposals
    assert "BTCUSDT" not in verdict.blocked_proposals


def test_degraded_dominance_clean_when_not_injected(store) -> None:
    _content, ids = store
    read = SentimentRead(
        agent="influencer", coin="BTC", stance="bullish", level="positive",
        s=0.5, confidence=0.9,
        claims=[
            Claim(text="a", item_ids=[ids["degraded"], ids["btc_a"]], coins=["BTC"]),
        ],
        rationale="Bullish chatter.", as_of_ts=AS_OF,
    )
    verdict = _run(store, reads=[read], degraded_sources=None)
    assert "degraded_source_dominance" not in verdict.mismatches
    assert verdict.blocked_proposals == []


# --------------------------------------------------------------------------- #
# malformed input must FAIL CLOSED on a traded coin, never raise               #
# --------------------------------------------------------------------------- #


def test_malformed_store_fails_closed_on_traded_coin(store, monkeypatch) -> None:
    import futures_fund.sentiment_audit as audit_mod

    # a store that raises on every resolve must NOT crash the audit loop; on a TRADED coin a
    # check that cannot re-derive ground truth FAILS CLOSED (fatal), never opens.
    def _boom(*_a, **_k):
        raise RuntimeError("torn store")

    monkeypatch.setattr(audit_mod, "get_item", _boom)
    _content, ids = store
    verdict = _run(store)  # BTC traded
    assert verdict.passed is False  # fail-closed, no exception escaped


# --------------------------------------------------------------------------- #
# check 7: price-leak regex must not false-positive on sentiment prose         #
# --------------------------------------------------------------------------- #


def test_clean_sentiment_support_prose_passes_check_7(store) -> None:
    for clean in (
        "broad community support for the upgrade",
        "fading support for the governance proposal",
        "Strong resistance from the community to the contentious fork",
        "Looking at the chart it is bullish",
    ):
        read = _btc_read(store[1])
        read.rationale = clean
        verdict = _run(store, reads=[read])
        assert "no_directional_price_leak" not in verdict.mismatches, clean


def test_price_level_language_still_fails_check_7(store) -> None:
    leaky = _btc_read(store[1])
    leaky.rationale = "Long because price is holding the $63k support level."
    verdict = _run(store, reads=[leaky])
    assert verdict.passed is False
    assert "no_directional_price_leak" in verdict.mismatches


# --------------------------------------------------------------------------- #
# the deterministic, fail-closed gate                                          #
# --------------------------------------------------------------------------- #


def test_audit_gate_ok_false_when_missing(tmp_path: Path) -> None:
    assert audit_gate_ok(tmp_path / "state", cycle=7) is False


def test_audit_gate_ok_true_when_passed(tmp_path: Path, store) -> None:
    from futures_fund.cycle_io import save_output

    _content, ids = store
    verdict = _run(store, **_multi(ids))
    assert verdict.passed is True
    save_output(tmp_path / "state", 7, "auditor", verdict)
    assert audit_gate_ok(tmp_path / "state", cycle=7) is True


def test_audit_gate_ok_false_when_failed(tmp_path: Path) -> None:
    from futures_fund.cycle_io import save_output

    verdict = AuditVerdict(
        passed=False,
        checks=[AuditCheck(name="claim_citations_exist", passed=False)],
        mismatches=["claim_citations_exist"],
    )
    save_output(tmp_path / "state", 3, "auditor", verdict)
    assert audit_gate_ok(tmp_path / "state", cycle=3) is False


def test_audit_gate_ok_false_when_unparseable(tmp_path: Path) -> None:
    from futures_fund.cycle_io import cycle_dir

    d = cycle_dir(tmp_path / "state", 9)
    d.mkdir(parents=True, exist_ok=True)
    (d / "auditor.json").write_text("{ not valid json")
    assert audit_gate_ok(tmp_path / "state", cycle=9) is False


def _write_auditor_raw(state_dir: Path, cycle: int, raw: str) -> None:
    from futures_fund.cycle_io import cycle_dir

    d = cycle_dir(state_dir, cycle)
    d.mkdir(parents=True, exist_ok=True)
    (d / "auditor.json").write_text(raw)


@pytest.mark.parametrize("raw", ["[1, 2, 3]", '"x"', "42", "null", "true"])
def test_audit_gate_ok_fail_closed_on_valid_non_dict_json(tmp_path: Path, raw: str) -> None:
    _write_auditor_raw(tmp_path / "state", 11, raw)
    assert audit_gate_ok(tmp_path / "state", cycle=11) is False


@pytest.mark.parametrize(
    "raw",
    ['{"passed": "yes"}', '{"passed": "false"}', '{"passed": 1}', '{"passed": ["x"]}'],
)
def test_audit_gate_ok_requires_strict_true(tmp_path: Path, raw: str) -> None:
    _write_auditor_raw(tmp_path / "state", 13, raw)
    assert audit_gate_ok(tmp_path / "state", cycle=13) is False


# --------------------------------------------------------------------------- #
# directional decision must REST on at least one sentiment read (no hallucination) #
# --------------------------------------------------------------------------- #


def test_directional_proposal_with_no_reads_fails(store) -> None:
    content_dir, _ids = store
    plan = ResearchPlan(
        symbol="DOGE", rating="strong_long", confidence=0.95,
        thesis="DOGE moons.", falsifiable_prediction="DOGE up 24h.",
    )
    prop = AgentProposal(
        symbol="DOGEUSDT", direction="long", entry=1.0, stop=0.9,
        take_profits=[1.2], atr=0.1, confidence=0.95, rationale="Bullish.",
    )
    verdict = review_cycle(
        content_dir, reads=[], plans=[plan], proposals=[prop], now=NOW
    )
    assert verdict.passed is False
    assert "evidence_grounding" in verdict.mismatches


def test_directional_proposal_with_other_coin_reads_fails(store) -> None:
    content_dir, ids = store
    # a DOGE proposal grounded only by a BTC read is ungrounded for DOGE (and DOGE IS traded here).
    btc_read = _btc_read(ids)
    plan = ResearchPlan(
        symbol="DOGE", rating="long", confidence=0.9,
        thesis="DOGE up.", falsifiable_prediction="DOGE up.",
    )
    prop = AgentProposal(
        symbol="DOGEUSDT", direction="long", entry=1.0, stop=0.9,
        take_profits=[1.2], atr=0.1, confidence=0.9, rationale="Bullish.",
    )
    verdict = review_cycle(
        content_dir, reads=[btc_read], plans=[plan], proposals=[prop], now=NOW
    )
    assert verdict.passed is False
    assert "evidence_grounding" in verdict.mismatches
