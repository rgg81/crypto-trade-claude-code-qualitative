from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path


def _path(state_dir) -> Path:
    return Path(state_dir) / "equity-history.jsonl"


def record_equity(state_dir, ts: datetime, equity: float, cycle: int) -> None:
    """Append — or REPLACE the existing point for this `cycle` — the desk's total equity at cycle
    end (the return series' source). Idempotent under a DUE RETRY re-running the same cycle: without
    this, a RETRY appended a SECOND point for the cycle, injecting a spurious ~0% return that
    corrupts the Sharpe/Sortino and the daily/weekly/monthly circuit breakers fed off this series.
    Rewrite is atomic (tmp + os.replace) and tolerant of a pre-existing malformed line."""
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    recs = []
    if p.exists():
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue  # skip a corrupt line rather than wedge the whole series
            if isinstance(r, dict) and r.get("cycle") == cycle:
                continue  # drop the prior point for this cycle -> RETRY replaces, never duplicates
            recs.append(r)
    recs.append({"ts": ts.isoformat(), "equity": float(equity), "cycle": cycle})
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("".join(json.dumps(r, default=str) + "\n" for r in recs))
    os.replace(tmp, p)


def equity_series(state_dir) -> list[tuple[str, float]]:
    p = _path(state_dir)
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if line.strip():
            r = json.loads(line)
            out.append((r["ts"], float(r["equity"])))
    return out


def returns_series(state_dir) -> list[float]:
    eq = [e for _, e in equity_series(state_dir)]
    return [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq)) if eq[i - 1] > 0]


def period_return(state_dir, now: datetime, days: float) -> float:
    """Return over the trailing `days`: latest equity vs the last equity at/before now-days
    (or the earliest on record if none is that old). 0.0 with < 2 points. Feeds the A1
    circuit breakers (daily/weekly/monthly)."""
    series = [(datetime.fromisoformat(ts), eq) for ts, eq in equity_series(state_dir)]
    if len(series) < 2:
        return 0.0
    cutoff = now - timedelta(days=days)
    older = [eq for ts, eq in series if ts <= cutoff]
    base = older[-1] if older else series[0][1]
    last = series[-1][1]
    return (last / base - 1.0) if base > 0 else 0.0
