import json
from datetime import UTC, datetime

from futures_fund.repair import is_protected, log_error, record_repair


def test_is_protected_flags_risk_and_exec_modules():
    assert is_protected("futures_fund/risk_gate.py") is True
    assert is_protected("futures_fund/executor.py") is True
    assert is_protected("cycle.py") is True
    assert is_protected("futures_fund/brief.py") is False
    assert is_protected("futures_fund/news.py") is False


def test_log_error_appends_jsonl(tmp_path):
    log_error(tmp_path, phase="execute", command="gate_execute_cli", error="boom",
              traceback="Traceback...", ts=datetime(2026, 5, 1, tzinfo=UTC))
    log_error(tmp_path, phase="screen", command="screen_cli", error="bad json",
              ts=datetime(2026, 5, 1, 1, tzinfo=UTC))
    lines = [
        json.loads(x)
        for x in (tmp_path / "error-log.jsonl").read_text().splitlines()
        if x.strip()
    ]
    assert len(lines) == 2
    assert lines[0]["phase"] == "execute" and lines[0]["error"] == "boom"


def test_record_repair_appends_structured_entry(tmp_path):
    record_repair(tmp_path, symptom="screen crashed on dict input",
                  root_cause="analyst reports saved dict-wrapped",
                  fix="screen_step tolerates dict", verification="186 tests green",
                  ts=datetime(2026, 5, 1, tzinfo=UTC))
    md = (tmp_path / "repair-journal.md").read_text()
    assert "Symptom" in md and "Root cause" in md and "Verification" in md
    assert "screen crashed on dict input" in md
