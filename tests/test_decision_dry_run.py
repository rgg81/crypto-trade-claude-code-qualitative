"""End-to-end OFFLINE dry-run of the DETERMINISTIC decision SPINE (Rule 4).

This is the integration seam the unit tests (``test_audit_cli`` and ``test_gate_wiring``) stub past:
each of those seeds the OTHER half by hand (the gate test hand-writes ``auditor.json``; the audit
test never executes). Here we drive the REAL two-stage spine end-to-end with NO LLM and NO network:

  1. seed a content store with a few crafted, SUMMARIZED :class:`ContentItem` s for two coins;
  2. hand-author this cycle's ``sentiment_reads.json`` (citing those REAL item ids), ``plans.json``,
     ``proposals.json`` (one valid long) and ``context.json`` (spec/mark/atr/funding) + account;
  3. run :func:`scripts.audit_cli.run` -> writes a GENUINE ``auditor.json`` (passed=True);
  4. run :func:`scripts.gate_execute_cli.gate_execute` which reads THAT verdict via the fail-closed
     gate -> the position is OPENED and a Phase-1 journal decision is written.

Then a SECOND scenario corrupts ONE citation so the REAL auditor FAILS — and the gate, reading that
real failed verdict, opens NOTHING but still writes a report. Both halves are asserted, proving the
producer (auditor) and consumer (gate) agree on the on-disk ``auditor.json`` contract.

Every timestamp/price/spec figure is injected; the auditor's PIT check uses each read's ``as_of_ts``
and the gate's prices come from ``context.json`` — so the whole spine is deterministic and offline.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.config import Settings
from futures_fund.content_store import ContentItem, make_id, store_items
from futures_fund.cycle_io import cycle_dir, save_output
from futures_fund.journal import read_all_decisions
from futures_fund.models import MmrBracket, SymbolSpec
from futures_fund.sentiment_audit import audit_gate_ok
from futures_fund.state import load_positions
from scripts.audit_cli import run as audit_run
from scripts.gate_execute_cli import gate_execute

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
AS_OF = NOW
PAST = NOW - timedelta(hours=6)  # PIT-clean: published strictly before the decision anchor
CYCLE = 7

# Realistic price geometry shared by the proposal AND the context mark so the SAME artifact
# satisfies BOTH the anti-hallucination auditor and the risk gate's RR / liq-distance rules.
ENTRY, STOP, TP = 65_000.0, 63_000.0, 71_000.0   # RR = 6000/2000 = 3.0 (>> 2.0 floor)
MARK, ATR, FUNDING = 65_000.0, 1_500.0, 0.0


# --------------------------------------------------------------------------- #
# content store: SUMMARIZED items for two coins (BTC across two sources)        #
# --------------------------------------------------------------------------- #
def _mk_item(*, source: str, url: str, title: str, coins: list[str], summary: str) -> ContentItem:
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
        summary=summary,                 # SUMMARIZED (analyst has processed it)
        item_sentiment="positive",
        summarized_ts=PAST,
    )


def _seed_store(content_dir: Path) -> dict[str, str]:
    """Two BTC items across two distinct sources (the §5 sufficiency floor needs >=2 items /
    >=2 sources for a high-conviction directional read) plus an ETH item, all summarized."""
    btc_a = _mk_item(
        source="coindesk", url="https://coindesk.example/btc-a",
        title="BTC ETF inflows accelerate", coins=["BTC"],
        summary="Spot ETF net inflows hit a multi-week high as demand broadens.",
    )
    btc_b = _mk_item(
        source="theblock", url="https://theblock.example/btc-b",
        title="Whales accumulate BTC", coins=["BTC"],
        summary="On-chain data shows large wallets steadily accumulating over the week.",
    )
    eth_a = _mk_item(
        source="coindesk", url="https://coindesk.example/eth-a",
        title="ETH staking inflows steady", coins=["ETH"],
        summary="Staking deposits remain steady; narrative neutral-to-constructive.",
    )
    store_items(str(content_dir), [btc_a, btc_b, eth_a])
    return {"btc_a": btc_a.id, "btc_b": btc_b.id, "eth_a": eth_a.id}


# --------------------------------------------------------------------------- #
# hand-authored cycle artifacts (the sentiment funnel's output)                 #
# --------------------------------------------------------------------------- #
def _reads(ids: dict[str, str]) -> list[dict]:
    """One high-conviction bullish BTC read citing the two REAL BTC items (2 sources), plus a
    neutral ETH read. No price/TA language (the §7 direction-is-sentiment-only mandate)."""
    return [
        {
            "agent": "flow", "coin": "BTC", "stance": "bullish", "level": "positive",
            "s": 0.5, "confidence": 0.8,
            "claims": [
                {"text": "ETF inflows", "item_ids": [ids["btc_a"]], "coins": ["BTC"]},
                {"text": "whale accumulation", "item_ids": [ids["btc_b"]], "coins": ["BTC"]},
            ],
            "rationale": "Strong on-chain accumulation and ETF demand point bullish.",
            "as_of_ts": AS_OF.isoformat(),
        },
        {
            "agent": "narrative", "coin": "ETH", "stance": "neutral", "level": "neutral",
            "s": 0.0, "confidence": 0.4,
            "claims": [{"text": "steady staking", "item_ids": [ids["eth_a"]], "coins": ["ETH"]}],
            "rationale": "Staking narrative is steady; no decisive lean either way.",
            "as_of_ts": AS_OF.isoformat(),
        },
    ]


def _plans() -> list[dict]:
    return [
        {
            "symbol": "BTC", "rating": "long", "confidence": 0.8,
            "thesis": "Net-positive flow and bullish narrative justify a long.",
            "falsifiable_prediction": "BTC sentiment stays net-positive over the next 24h.",
        },
        {
            "symbol": "ETH", "rating": "flat", "confidence": 0.4,
            "thesis": "Mixed staking narrative; stand aside.",
            "falsifiable_prediction": "ETH sentiment stays mixed over the next 24h.",
        },
    ]


def _proposals_doc() -> dict:
    return {
        "proposals": [
            {
                "symbol": "BTCUSDT", "direction": "long",
                "entry": ENTRY, "stop": STOP, "take_profits": [TP], "atr": ATR,
                "confidence": 0.8,
                "rationale": "Direction driven by bullish flow + narrative sentiment.",
                "falsifiable_prediction": "Sentiment remains net-positive.",
                "contributing_agents": ["flow", "narrative"],
            }
        ],
        "management": [],
        "triggers": [],
        "cancel_triggers": [],
    }


def _spec(symbol: str = "BTCUSDT") -> dict:
    return SymbolSpec(
        symbol=symbol, tick_size=0.01, step_size=0.001, min_notional=5.0,
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12,
                                 mmr=0.004, maint_amount=0.0, max_leverage=125.0)],
    ).model_dump()


def _context() -> dict:
    return {
        "crowd_mood": {"mood": "greedy", "dispersion": 0.2},
        "symbols": {"BTCUSDT": {"spec": _spec(), "mark": MARK, "atr": ATR,
                                "funding_rate": FUNDING}},
        "pnl": {"daily_pct": 0.0, "weekly_pct": 0.0, "monthly_pct": 0.0},
        "scorecard": {"recent_hit_rate": 0.55},
    }


def _settings() -> Settings:
    return Settings(account_size_usdt=10_000.0, symbols=["BTC/USDT:USDT"], timeframe="4h")


def _seed_cycle(state_dir: Path, *, reads: list[dict], proposals: dict) -> None:
    """Write the four cycle artifacts the spine consumes: the three audited ones plus context."""
    cdir = cycle_dir(str(state_dir), CYCLE)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "sentiment_reads.json").write_text(json.dumps(reads))
    (cdir / "plans.json").write_text(json.dumps(_plans()))
    (cdir / "proposals.json").write_text(json.dumps(proposals))
    save_output(str(state_dir), CYCLE, "context", _context())


def _drive_spine(state_dir: Path, content_dir: Path, memory_dir: Path) -> dict:
    """Run the REAL spine: audit_cli.run (writes auditor.json) -> gate_execute (reads it)."""
    audit_run(str(state_dir), str(content_dir), CYCLE, NOW)
    return gate_execute(
        str(state_dir), str(memory_dir), CYCLE, NOW,
        settings=_settings(), exchange=None,
    )


# --------------------------------------------------------------------------- #
# SCENARIO A — clean spine: auditor PASSES, gate OPENS + journals               #
# --------------------------------------------------------------------------- #
def test_clean_spine_audits_then_opens_and_journals(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    content_dir = tmp_path / "content"
    memory_dir = tmp_path / "memory"
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, reads=_reads(ids), proposals=_proposals_doc())

    report = _drive_spine(state_dir, content_dir, memory_dir)

    # --- the AUDITOR produced a genuine PASS at the path the gate reads -------------------------
    auditor = json.loads((cycle_dir(str(state_dir), CYCLE) / "auditor.json").read_text())
    assert auditor["passed"] is True, auditor.get("mismatches")
    assert len(auditor["checks"]) == 9            # all nine ground-truth checks ran
    assert audit_gate_ok(str(state_dir), CYCLE) is True

    # --- the GATE opened the position off that real verdict -------------------------------------
    assert report["audit_ok"] is True
    assert report["vetoed"] == []
    assert len(report["opened"]) == 1
    assert report["opened"][0]["symbol"] == "BTCUSDT"
    assert report["opened"][0]["direction"] == "long"

    positions = load_positions(str(state_dir))
    assert len(positions) == 1
    assert positions[0].symbol == "BTCUSDT"
    assert positions[0].direction == "long"

    # --- a Phase-1 journal decision was written, carrying the sentiment provenance --------------
    decisions = read_all_decisions(str(memory_dir))
    assert len(decisions) == 1
    d = decisions[0]
    assert d["symbol"] == "BTCUSDT" and d["cycle"] == CYCLE
    assert d["contributing_agents"] == ["flow", "narrative"]
    assert d["falsifiable_prediction"] == "Sentiment remains net-positive."
    assert d["crowd_mood"] == "greedy"
    assert d["id"] == positions[0].decision_id    # the open is linked to its decision

    # --- report.json persisted with the scheduler run-markers -----------------------------------
    assert (cycle_dir(str(state_dir), CYCLE) / "report.json").exists()
    assert "candle" in report and "ran_at" in report


# --------------------------------------------------------------------------- #
# SCENARIO B — corrupted citation: auditor FAILS, gate OPENS NOTHING            #
# --------------------------------------------------------------------------- #
def test_corrupt_citation_fails_audit_and_gate_opens_nothing(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    content_dir = tmp_path / "content"
    memory_dir = tmp_path / "memory"
    ids = _seed_store(content_dir)

    # Corrupt exactly ONE citation: the first BTC claim now cites a FABRICATED id no item carries,
    # so the auditor's check 1 (claim_citations_exist) re-resolves it against the store and FAILS.
    reads = _reads(ids)
    reads[0]["claims"][0]["item_ids"] = ["sha256:deadbeef-not-real"]
    _seed_cycle(state_dir, reads=reads, proposals=_proposals_doc())

    report = _drive_spine(state_dir, content_dir, memory_dir)

    # --- the AUDITOR caught the hallucinated citation and persisted a HALT ----------------------
    auditor = json.loads((cycle_dir(str(state_dir), CYCLE) / "auditor.json").read_text())
    assert auditor["passed"] is False
    assert "claim_citations_exist" in auditor["mismatches"]
    assert audit_gate_ok(str(state_dir), CYCLE) is False

    # --- the GATE, reading that real failed verdict, opened NOTHING but still wrote a report ----
    assert report["audit_ok"] is False
    assert report["reason"] == "audit veto"
    assert report["opened"] == []
    assert load_positions(str(state_dir)) == []
    assert read_all_decisions(str(memory_dir)) == []      # nothing journaled under a veto
    assert any("AUDIT VETO" in w for w in report["warnings"])

    # the report is STILL written (the gate halts, it does not vanish)
    assert (cycle_dir(str(state_dir), CYCLE) / "report.json").exists()
