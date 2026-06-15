"""Offline tests for scripts/decision_qa_cli.py — the DECISION-QA CLI.

NO live network: a tmp content store + crafted cycle artifacts + a tmp memory dir. We assert the CLI
(a) APPENDS the DecisionQA as one JSON line to memory/decision-qa.jsonl, (b) updates
memory/agent_reliability.json, (c) prints a one-line summary with per-lane hallucination counts +
reliability, and (d) is fail-soft (a missing artifact never crashes, exit 0).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.content_store import ContentItem, make_id, store_items
from futures_fund.cycle_io import cycle_dir
from scripts.decision_qa_cli import main, run

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
PAST = NOW - timedelta(hours=6)


def _mk_item(*, source, url, title, coins) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title), source=source, feed=f"https://{source}.example/rss",
        url=url, title=title, body="", coins=coins, published_ts=PAST, fetched_ts=PAST,
    )


def _seed_store(content_dir: Path) -> dict[str, str]:
    a = _mk_item(source="coindesk", url="https://x.example/a", title="BTC up", coins=["BTC"])
    b = _mk_item(source="theblock", url="https://x.example/b", title="BTC whales", coins=["BTC"])
    store_items(str(content_dir), [a, b])
    return {"a": a.id, "b": b.id}


def _read(*, agent, coin, stance, level, s, confidence, item_ids, coins):
    return {
        "agent": agent, "coin": coin, "stance": stance, "level": level, "s": s,
        "confidence": confidence,
        "claims": [{"text": "c", "item_ids": item_ids, "coins": coins}],
        "rationale": "r", "as_of_ts": NOW.isoformat(),
    }


def _seed_cycle(state_dir: Path, cycle: int, reads: list[dict]) -> None:
    cdir = cycle_dir(str(state_dir), cycle)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "sentiment_reads.json").write_text(json.dumps(reads))


def test_cli_appends_jsonl_and_updates_reliability(tmp_path: Path) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)
    _seed_cycle(state_dir, 1, [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.8, item_ids=[ids["a"]], coins=["BTC"]),
    ])

    rc = main(["--cycle", "1", "--state-dir", str(state_dir),
               "--content-dir", str(content_dir), "--memory-dir", str(memory_dir)])
    assert rc == 0

    jsonl = memory_dir / "decision-qa.jsonl"
    assert jsonl.exists()
    lines = [ln for ln in jsonl.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["cycle"] == 1
    assert rec["lanes"]["flow"]["hallucinated_citations"] == 0

    rel = json.loads((memory_dir / "agent_reliability.json").read_text())
    assert rel["flow"]["reliability"] > 0.9


def test_cli_appends_one_line_per_cycle(tmp_path: Path) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    ids = _seed_store(content_dir)
    for c in (1, 2, 3):
        _seed_cycle(state_dir, c, [
            _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
                  confidence=0.8, item_ids=[ids["a"]], coins=["BTC"]),
        ])
        run(str(state_dir), str(content_dir), str(memory_dir), c)

    lines = [ln for ln in (memory_dir / "decision-qa.jsonl").read_text().splitlines() if ln.strip()]
    assert len(lines) == 3
    assert [json.loads(ln)["cycle"] for ln in lines] == [1, 2, 3]


def test_cli_prints_one_line_summary(tmp_path: Path, capsys) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    _seed_store(content_dir)
    # a hallucinating flow read so the summary shows halluc>0
    _seed_cycle(state_dir, 5, [
        _read(agent="flow", coin="BTC", stance="bullish", level="positive", s=0.5,
              confidence=0.9, item_ids=["fake-id"], coins=["BTC"]),
    ])
    rc = main(["--cycle", "5", "--state-dir", str(state_dir),
               "--content-dir", str(content_dir), "--memory-dir", str(memory_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DECISION-QA cycle 5" in out
    assert "flow:halluc=1" in out
    assert "rel=" in out
    # exactly one printed line
    assert len([ln for ln in out.splitlines() if ln.strip()]) == 1


def test_cli_fail_soft_on_missing_cycle(tmp_path: Path, capsys) -> None:
    state_dir, content_dir, memory_dir = (
        tmp_path / "state", tmp_path / "content", tmp_path / "memory")
    _seed_store(content_dir)
    # cycle 42 has no artifacts at all
    rc = main(["--cycle", "42", "--state-dir", str(state_dir),
               "--content-dir", str(content_dir), "--memory-dir", str(memory_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DECISION-QA cycle 42" in out
    assert "auditor=n/a" in out
    # still appended a (zeroed) line + wrote reliability
    assert (memory_dir / "decision-qa.jsonl").exists()
    assert (memory_dir / "agent_reliability.json").exists()
