from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace (atomic rename) — a crash mid-write leaves the PRIOR file
    intact rather than a half-written one. Same pattern as state.py / cycle_io.py."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class SourceStat(BaseModel):
    """Rolling health record for one crawl source. Timestamps are ISO-8601 strings (tz-aware)."""

    consecutive_failures: int = 0
    last_ok_ts: str | None = None
    last_latency_ms: float | None = None
    total_ok: int = 0
    total_err: int = 0
    disabled_until: str | None = None


class SourceHealth(BaseModel):
    """Maps source_name -> SourceStat. The on-disk shape is the inner dict (sources)."""

    sources: dict[str, SourceStat] = Field(default_factory=dict)

    def stat(self, source: str) -> SourceStat:
        """Return the (mutable) stat for `source`, creating a fresh one on first touch."""
        s = self.sources.get(source)
        if s is None:
            s = SourceStat()
            self.sources[source] = s
        return s


def _health_path(state_dir) -> Path:
    return Path(state_dir) / "crawl" / "source_health.json"


def _parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def load_health(state_dir) -> SourceHealth:
    """Load source_health.json (state/crawl/source_health.json). Missing/corrupt -> empty health."""
    p = _health_path(state_dir)
    if not p.exists():
        return SourceHealth()
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return SourceHealth()
    return SourceHealth(sources={k: SourceStat.model_validate(v) for k, v in raw.items()})


def save_health(state_dir, health: SourceHealth) -> None:
    """Persist health atomically as {source_name: {stat...}}."""
    payload = {k: json.loads(v.model_dump_json()) for k, v in health.sources.items()}
    _atomic_write_text(_health_path(state_dir), json.dumps(payload, indent=2))


def record_ok(health: SourceHealth, source: str, latency_ms: float, now: datetime) -> SourceHealth:
    """Record a successful fetch: clears the failure streak, lifts any circuit breaker, stamps
    last_ok_ts/latency, bumps total_ok. Returns `health` (mutated in place) for chaining."""
    s = health.stat(source)
    s.consecutive_failures = 0
    s.last_ok_ts = now.isoformat()
    s.last_latency_ms = float(latency_ms)
    s.total_ok += 1
    s.disabled_until = None
    return health


def record_err(
    health: SourceHealth,
    source: str,
    now: datetime,
    k_threshold: int = 3,
    base_backoff_min: int = 15,
) -> SourceHealth:
    """Record a failed fetch. After `k_threshold` consecutive failures the source is circuit-broken:
    disabled_until = now + base_backoff_min * 2**(consecutive_failures - k_threshold) minutes
    (exponential backoff that grows with each further failure). Returns `health` (mutated)."""
    s = health.stat(source)
    s.consecutive_failures += 1
    s.total_err += 1
    if s.consecutive_failures >= k_threshold:
        backoff_min = base_backoff_min * (2 ** (s.consecutive_failures - k_threshold))
        s.disabled_until = (now + timedelta(minutes=backoff_min)).isoformat()
    return health


def is_healthy(health: SourceHealth, source: str, now: datetime) -> bool:
    """True unless the source is currently circuit-broken (now < disabled_until). Unknown->True."""
    s = health.sources.get(source)
    if s is None or not s.disabled_until:
        return True
    until = _parse_ts(s.disabled_until)
    if until is None:
        return True
    return now >= until


def healthy_sources(health: SourceHealth, all_sources: list[str], now: datetime) -> list[str]:
    """The subset of `all_sources` that are not currently circuit-broken (order preserved)."""
    return [src for src in all_sources if is_healthy(health, src, now)]


def degraded_sources(health: SourceHealth, now: datetime) -> set[str]:
    """The set of known sources currently circuit-broken (now < disabled_until)."""
    return {src for src in health.sources if not is_healthy(health, src, now)}
