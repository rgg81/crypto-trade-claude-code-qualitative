"""Offline tests for scripts/improvement_log_cli.py — the OVERSEER journal-append wrapper.

No network, no real clock dependence (the CLI's --ts pins the timestamp). The CLI must append a
record to BOTH memory/improvement-journal.{md,jsonl} via record_improvement, honouring the explicit
flags and a --json base, and pinning the timestamp when --ts is given.
"""
from __future__ import annotations

import json

from scripts.improvement_log_cli import main, run


def _rows(tmp_path):
    p = tmp_path / "improvement-journal.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


def test_run_appends_applied_record(tmp_path):
    from datetime import UTC, datetime

    entry = {
        "detected": "flow lane hallucinated 3 cycles running",
        "classification": "prompt",
        "target": "agents/flow_sentiment.md",
        "fix_summary": "tighten cite-only-real-ids rule",
        "applied": True,
        "surfaced": False,
        "test_result": "778 passed",
    }
    out = run(tmp_path, entry, datetime(2026, 6, 15, 12, 0, tzinfo=UTC))
    assert out["logged"] is True
    assert out["applied"] is True
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["target"] == "agents/flow_sentiment.md"
    assert rows[0]["ts"] == "2026-06-15T12:00:00+00:00"
    md = (tmp_path / "improvement-journal.md").read_text()
    assert "auto-applied" in md


def test_main_flags_build_entry_and_pin_ts(tmp_path):
    rc = main(
        [
            "--memory-dir",
            str(tmp_path),
            "--detected",
            "narrative lane decaying reliability",
            "--classification",
            "prompt",
            "--target",
            "agents/narrative_sentiment.md",
            "--fix",
            "add a stale-narrative guard",
            "--applied",
            "--test-result",
            "778 passed",
            "--ts",
            "2026-06-15T16:00:00+00:00",
        ]
    )
    assert rc == 0
    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["classification"] == "prompt"
    assert rows[0]["applied"] is True
    assert rows[0]["surfaced"] is False
    assert rows[0]["ts"] == "2026-06-15T16:00:00+00:00"


def test_main_surfaced_protected_record(tmp_path):
    rc = main(
        [
            "--memory-dir",
            str(tmp_path),
            "--detected",
            "recurring auditor advisory",
            "--classification",
            "protected",
            "--target",
            "futures_fund/sentiment_audit.py",
            "--fix",
            "surface to human — never auto-applied",
            "--surfaced",
            "--ts",
            "2026-06-15T16:00:00+00:00",
        ]
    )
    assert rc == 0
    rows = _rows(tmp_path)
    assert rows[0]["surfaced"] is True
    assert rows[0]["applied"] is False
    assert rows[0]["target"] == "futures_fund/sentiment_audit.py"
    md = (tmp_path / "improvement-journal.md").read_text()
    assert "surfaced to human" in md


def test_main_json_base_with_flag_override(tmp_path):
    base = json.dumps(
        {
            "detected": "from json",
            "classification": "code",
            "target": "futures_fund/decision_qa.py",
            "fix_summary": "from json fix",
            "applied": True,
        }
    )
    rc = main(
        [
            "--memory-dir",
            str(tmp_path),
            "--json",
            base,
            "--fix",
            "overridden fix",  # override the json base's fix_summary
            "--ts",
            "2026-06-15T16:00:00+00:00",
        ]
    )
    assert rc == 0
    rows = _rows(tmp_path)
    assert rows[0]["detected"] == "from json"
    assert rows[0]["fix_summary"] == "overridden fix"
    assert rows[0]["applied"] is True  # preserved from the json base (flag absent)
