"""Offline tests for scripts/due_check.py — the hourly-poll due-gate CLI.

It is a thin print wrapper over scheduling.cycle_due (which is pure on-disk I/O), so these tests
drive it against a temp state dir with NO network and assert the printed line + exit code:
  DUE FRESH/RETRY <n>  (exit 0) | SKIP: ...  (exit 0) | ERROR: ...  (exit 2)."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.due_check import main


def _seed_report(state_dir: Path, n: int, candle_iso: str) -> None:
    d = state_dir / "cycle" / str(n)
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.json").write_text(json.dumps({"candle": candle_iso}))


def test_cold_start_prints_due_fresh_1(tmp_path: Path, capsys) -> None:
    rc = main([str(tmp_path / "state")])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == "DUE FRESH 1"


def test_already_served_candle_prints_skip(tmp_path: Path, capsys) -> None:
    state_dir = tmp_path / "state"
    # Serve the current candle so the gate decides SKIP.
    boundary = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    boundary = boundary.replace(hour=(boundary.hour // 4) * 4)
    _seed_report(state_dir, 1, boundary.isoformat())

    rc = main([str(state_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("SKIP:")


def test_error_path_returns_exit_2(monkeypatch, capsys) -> None:
    # Force the import inside main() to fail -> ERROR line, exit code 2 (fail visible, not silent).
    import builtins

    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "futures_fund.scheduling":
            raise ImportError("simulated import failure")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    rc = main(["state"])
    out = capsys.readouterr().out
    assert rc == 2
    assert out.startswith("ERROR:")
