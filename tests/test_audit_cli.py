"""Offline tests for scripts/audit_cli.py — the anti-hallucination AUDITOR CLI (Phase 8).

Every test runs with NO live network: a tmp content store seeded with crafted items + crafted
reads/plans/proposals JSON under state/cycle/<N>/, and an INJECTED `now`. We assert the CLI
(a) writes ``auditor.json`` where ``audit_gate_ok`` reads it, (b) writes ``passed=True`` for a fully
valid + grounded cycle, (c) writes ``passed=False`` for a hallucinated citation, and (d) FAIL-CLOSED
still writes a FAILED verdict (never crashes) when an audited input file is missing or malformed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.content_store import ContentItem, make_id, store_items
from futures_fund.cycle_io import cycle_dir
from futures_fund.sentiment_audit import audit_gate_ok
from scripts.audit_cli import main, run

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
AS_OF = NOW
PAST = NOW - timedelta(hours=6)  # PIT-clean: published before the decision anchor


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
    """Two distinct BTC items across two distinct sources (enough for the §5 sufficiency floor)."""
    btc_a = _mk_item(
        source="coindesk", url="https://coindesk.example/btc-a",
        title="BTC ETF inflows accelerate", coins=["BTC"],
    )
    btc_b = _mk_item(
        source="theblock", url="https://theblock.example/btc-b",
        title="Whales accumulate BTC", coins=["BTC"],
    )
    store_items(str(content_dir), [btc_a, btc_b])
    return {"btc_a": btc_a.id, "btc_b": btc_b.id}


def _valid_reads(ids: dict[str, str]) -> list[dict]:
    return [
        {
            "agent": "flow",
            "coin": "BTC",
            "stance": "bullish",
            "level": "positive",
            "s": 0.5,
            "confidence": 0.8,
            "claims": [
                {"text": "ETF inflows", "item_ids": [ids["btc_a"]], "coins": ["BTC"]},
                {"text": "whale accumulation", "item_ids": [ids["btc_b"]], "coins": ["BTC"]},
            ],
            "rationale": "Strong on-chain accumulation and ETF demand point bullish.",
            "as_of_ts": AS_OF.isoformat(),
        }
    ]


def _valid_plans() -> list[dict]:
    return [
        {
            "symbol": "BTC",
            "rating": "long",
            "confidence": 0.8,
            "thesis": "Net-positive flow and bullish narrative justify a long.",
            "falsifiable_prediction": "BTC sentiment stays net-positive over the next 24h.",
        }
    ]


def _valid_proposals_doc() -> dict:
    return {
        "proposals": [
            {
                "symbol": "BTCUSDT",
                "direction": "long",
                "entry": 65000.0,
                "stop": 63000.0,
                "take_profits": [68000.0],
                "atr": 1500.0,
                "confidence": 0.8,
                "rationale": "Direction driven by bullish flow + narrative sentiment.",
                "falsifiable_prediction": "Sentiment remains net-positive.",
            }
        ]
    }


def _seed_cycle(
    state_dir: Path,
    cycle: int,
    *,
    reads: list[dict] | None,
    plans: list[dict] | None,
    proposals: dict | None,
) -> None:
    """Write the three audited artifacts (None = intentionally omitted) under state/cycle/<N>/."""
    cdir = cycle_dir(str(state_dir), cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    if reads is not None:
        (cdir / "sentiment_reads.json").write_text(json.dumps(reads))
    if plans is not None:
        (cdir / "plans.json").write_text(json.dumps(plans))
    if proposals is not None:
        (cdir / "proposals.json").write_text(json.dumps(proposals))


def _read_auditor(state_dir: Path, cycle: int) -> dict:
    return json.loads((cycle_dir(str(state_dir), cycle) / "auditor.json").read_text())


# --------------------------------------------------------------------------- #
# happy path: a fully valid + grounded cycle PASSES and is persisted           #
# --------------------------------------------------------------------------- #


def test_valid_cycle_passes_and_writes_auditor(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(
        state_dir, 7,
        reads=_valid_reads(ids), plans=_valid_plans(), proposals=_valid_proposals_doc(),
    )

    verdict = run(str(state_dir), str(content_dir), 7, NOW)

    assert verdict.passed is True, verdict.mismatches
    assert verdict.mismatches == []
    assert len(verdict.checks) == 9
    # persisted exactly where the execute gate reads it
    data = _read_auditor(state_dir, 7)
    assert data["passed"] is True
    assert audit_gate_ok(str(state_dir), 7) is True


def test_main_exit_code_zero_on_pass(tmp_path: Path, capsys) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(
        state_dir, 1,
        reads=_valid_reads(ids), plans=_valid_plans(), proposals=_valid_proposals_doc(),
    )
    rc = main(
        ["--cycle", "1", "--state-dir", str(state_dir), "--content-dir", str(content_dir),
         "--now", NOW.isoformat()]
    )
    assert rc == 0
    assert "PASS" in capsys.readouterr().out


def test_per_coin_plan_files_are_loaded(tmp_path: Path) -> None:
    """plan_<COIN>.json fallback: no combined plans.json, one per-coin plan object instead."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(
        state_dir, 4,
        reads=_valid_reads(ids), plans=None, proposals=_valid_proposals_doc(),
    )
    cdir = cycle_dir(str(state_dir), 4)
    (cdir / "plan_BTC.json").write_text(json.dumps(_valid_plans()[0]))

    verdict = run(str(state_dir), str(content_dir), 4, NOW)
    assert verdict.passed is True, verdict.mismatches


def test_degraded_sources_are_injected(tmp_path: Path) -> None:
    """A high-conviction TRADED read resting ONLY on a circuit-broken source trips check 8 — proving
    the CLI injects the source-health degraded set into the auditor. Under per-proposal scoping
    degraded-source dominance is PROPOSAL-BLOCKING (not a fatal whole-cycle veto): the verdict stays
    ``passed`` (no fabrication) but the specific proposal is BLOCKED so the gate still never opens
    it — the anti-hallucination protection on the actual trade is preserved, just scoped."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    # single item from one source so the read rests entirely on it
    item = _mk_item(
        source="randomblog", url="https://randomblog.example/btc",
        title="BTC to the moon", coins=["BTC"],
    )
    store_items(str(content_dir), [item])
    reads = [
        {
            "agent": "influencer", "coin": "BTC", "stance": "bullish", "level": "positive",
            "s": 0.5, "confidence": 0.9,
            "claims": [{"text": "moon", "item_ids": [item.id], "coins": ["BTC"]}],
            "rationale": "Sentiment is overwhelmingly bullish across the community.",
            "as_of_ts": AS_OF.isoformat(),
        }
    ]
    _seed_cycle(state_dir, 9, reads=reads, plans=_valid_plans(), proposals=_valid_proposals_doc())

    # circuit-break randomblog in source_health
    health_dir = state_dir / "crawl"
    health_dir.mkdir(parents=True, exist_ok=True)
    disabled_until = (NOW + timedelta(hours=2)).isoformat()
    (health_dir / "source_health.json").write_text(
        json.dumps({"randomblog": {"disabled_until": disabled_until}})
    )

    verdict = run(str(state_dir), str(content_dir), 9, NOW)
    # degraded dominance is blocking, not fatal: the check fires (injection proven) and the BTC
    # proposal is BLOCKED, but the cycle is not vetoed wholesale.
    assert "degraded_source_dominance" in verdict.mismatches
    assert "BTCUSDT" in verdict.blocked_proposals


# --------------------------------------------------------------------------- #
# a hallucinated citation FAILS (and is persisted as a halt)                   #
# --------------------------------------------------------------------------- #


def test_hallucinated_citation_fails(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)  # real items exist, but the read cites a fabricated id
    reads = [
        {
            "agent": "flow", "coin": "BTC", "stance": "bullish", "level": "positive",
            "s": 0.5, "confidence": 0.8,
            "claims": [
                {"text": "fabricated", "item_ids": ["sha256:deadbeef-not-real"], "coins": ["BTC"]},
            ],
            "rationale": "Bullish narrative.",
            "as_of_ts": AS_OF.isoformat(),
        }
    ]
    _seed_cycle(state_dir, 3, reads=reads, plans=_valid_plans(), proposals=_valid_proposals_doc())

    verdict = run(str(state_dir), str(content_dir), 3, NOW)
    assert verdict.passed is False
    assert "claim_citations_exist" in verdict.mismatches
    # the failed verdict IS persisted, so the gate halts
    assert _read_auditor(state_dir, 3)["passed"] is False
    assert audit_gate_ok(str(state_dir), 3) is False


# --------------------------------------------------------------------------- #
# fail-closed: missing / malformed inputs -> a FAILED verdict is still WRITTEN #
# --------------------------------------------------------------------------- #


def test_missing_proposals_writes_failed_verdict(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 5, reads=_valid_reads(ids), plans=_valid_plans(), proposals=None)

    verdict = run(str(state_dir), str(content_dir), 5, NOW)
    assert verdict.passed is False
    assert verdict.mismatches == ["inputs_loadable"]
    # written + fail-closed gate halts
    assert _read_auditor(state_dir, 5)["passed"] is False
    assert audit_gate_ok(str(state_dir), 5) is False


def test_missing_reads_writes_failed_verdict(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    _seed_store(content_dir)
    _seed_cycle(state_dir, 6, reads=None, plans=_valid_plans(), proposals=_valid_proposals_doc())

    verdict = run(str(state_dir), str(content_dir), 6, NOW)
    assert verdict.passed is False
    assert audit_gate_ok(str(state_dir), 6) is False


def test_missing_plans_writes_failed_verdict(tmp_path: Path) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 8, reads=_valid_reads(ids), plans=None, proposals=_valid_proposals_doc())
    # no plans.json and no plan_<coin>.json -> fail-closed

    verdict = run(str(state_dir), str(content_dir), 8, NOW)
    assert verdict.passed is False
    assert audit_gate_ok(str(state_dir), 8) is False


def test_malformed_proposals_json_writes_failed_verdict(tmp_path: Path) -> None:
    """A truncated / non-JSON proposals file is fail-closed, never a crash."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 11, reads=_valid_reads(ids), plans=_valid_plans(), proposals=None)
    (cycle_dir(str(state_dir), 11) / "proposals.json").write_text("{ not valid json ::")

    verdict = run(str(state_dir), str(content_dir), 11, NOW)
    assert verdict.passed is False
    assert verdict.mismatches == ["inputs_loadable"]
    assert audit_gate_ok(str(state_dir), 11) is False


def test_proposals_missing_proposals_key_writes_failed_verdict(tmp_path: Path) -> None:
    """Valid JSON object but no '.proposals' list is still fail-closed (schema drift)."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 12, reads=_valid_reads(ids), plans=_valid_plans(), proposals={})

    verdict = run(str(state_dir), str(content_dir), 12, NOW)
    assert verdict.passed is False
    assert verdict.mismatches == ["inputs_loadable"]


def test_main_exit_code_two_on_fail(tmp_path: Path, capsys) -> None:
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 2, reads=_valid_reads(ids), plans=_valid_plans(), proposals=None)
    rc = main(
        ["--cycle", "2", "--state-dir", str(state_dir), "--content-dir", str(content_dir),
         "--now", NOW.isoformat()]
    )
    assert rc == 2
    out = capsys.readouterr().out
    assert "FAIL" in out
    assert "inputs_loadable" in out


def test_empty_proposals_list_is_valid(tmp_path: Path) -> None:
    """A cycle that proposes NO trades (empty .proposals) is a legitimate flat cycle, not an error;
    with no proposals to ground, the valid reads/plans still pass."""
    state_dir, content_dir = tmp_path / "state", tmp_path / "content"
    ids = _seed_store(content_dir)
    # 'flat' plan so stance_consistency / grounding have no directional decision to fault
    flat_plan = [{
        "symbol": "BTC", "rating": "flat", "confidence": 0.5,
        "thesis": "Mixed signals.", "falsifiable_prediction": "BTC sentiment stays mixed.",
    }]
    _seed_cycle(
        state_dir, 13,
        reads=_valid_reads(ids), plans=flat_plan, proposals={"proposals": []},
    )
    verdict = run(str(state_dir), str(content_dir), 13, NOW)
    assert verdict.passed is True, verdict.mismatches
