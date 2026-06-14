from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from futures_fund.models import Direction


class Decision(BaseModel):
    """Two-phase decision record. Phase-1 fields written at decision time; Phase-2 (outcome)
    fields patched on close. extra='allow' lets Phase-B agents attach richer context."""

    model_config = ConfigDict(extra="allow")

    id: str
    ts: datetime
    cycle: int
    symbol: str
    direction: Direction
    entry: float
    stop: float
    # Phase-1 optional context
    take_profit: list[float] = Field(default_factory=list)
    size: float | None = None
    leverage: float | None = None
    r_multiple: float | None = None
    funding_at_entry: float | None = None
    regime: str | None = None
    setup: str | None = None
    alternatives_rejected: list[str] = Field(default_factory=list)
    key_assumptions: list[str] = Field(default_factory=list)
    falsifiable_prediction: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    dominant_signal: str | None = None
    contributing_agents: list[str] = Field(default_factory=list)
    retrieved_memory_ids: list[str] = Field(default_factory=list)
    # Phase-2 outcome (None until closed)
    exit_ts: datetime | None = None
    realized_pnl: float | None = None
    fees: float | None = None
    funding_paid: float | None = None
    slippage: float | None = None
    prediction_correct: bool | None = None
    low_level_lesson: str | None = None
    high_level_lesson: str | None = None
    importance_1_10: int | None = None
    # Partial scale-out banks (cy78 fix): each {pnl, fees, funding, fraction, price, ts}. The TRUE
    # realized for the trade is realized_pnl (final close) PLUS the sum of these — without this a
    # scale-out's banked cash was invisible to the journal (cy22 SOL +$119 cost the desk's best
    # short 44% of its record). Read the complete realized via realized_total().
    partial_banks: list[dict] = Field(default_factory=list)


def _episodic_dir(memory_dir) -> Path:
    return Path(memory_dir) / "episodic"


def journal_file(memory_dir, ts: datetime) -> Path:
    return _episodic_dir(memory_dir) / f"journal-{ts:%Y-%m}.jsonl"


def append_decision(memory_dir, fields: dict) -> str:
    """Validate and append a Phase-1 decision; returns its id (generated if absent).

    IDEMPOTENT per (cycle, symbol, direction): a DUE RETRY re-running the same cycle re-journals the
    same opens — without this guard that double-counts the open in hit-rate / per-agent stats /
    reflection. If a decision for this (cycle, symbol, direction) already exists, its id is returned
    and nothing is appended. The key is unique per cycle (no stacking: one open per symbol+direction
    per cycle, and cycle numbers are monotonic), so this never collides two legitimate decisions."""
    data = dict(fields)
    cyc, sym, dirn = data.get("cycle"), data.get("symbol"), data.get("direction")
    if cyc is not None and sym is not None:
        for d in read_all_decisions(memory_dir):
            if d.get("cycle") == cyc and d.get("symbol") == sym and d.get("direction") == dirn:
                return d.get("id")  # already journaled this cycle's open -> reuse, don't duplicate
    data.setdefault("id", uuid.uuid4().hex)
    decision = Decision.model_validate(data)
    f = journal_file(memory_dir, decision.ts)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        fh.write(decision.model_dump_json() + "\n")
    return decision.id


def _all_files(memory_dir) -> list[Path]:
    d = _episodic_dir(memory_dir)
    return sorted(d.glob("journal-*.jsonl")) if d.exists() else []


def read_all_decisions(memory_dir) -> list[dict]:
    """All decision records as raw dicts. NOTE: datetime fields (ts, exit_ts) are ISO-8601
    STRINGS here, not datetime objects — call Decision.model_validate(r) for typed access."""
    out: list[dict] = []
    for f in _all_files(memory_dir):
        for line in f.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def read_open_decisions(memory_dir) -> list[dict]:
    """Decisions without a realized outcome yet (Phase-2 not filled)."""
    return [r for r in read_all_decisions(memory_dir) if r.get("realized_pnl") is None]


def patch_outcome(memory_dir, decision_id: str, outcome: dict) -> bool:
    """Merge Phase-2 outcome fields into the decision with `decision_id`. Rewrites the
    containing monthly file. Returns False if the id is not found."""
    for f in _all_files(memory_dir):
        records = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        hit = False
        for r in records:
            if r.get("id") == decision_id:
                merged_dict = {**r, **outcome}
                # Auto-stamp r_multiple at close (cy78 journal-hygiene: 0/18 closes carried it). Use
                # the TRUE realized (final close + partial banks) / initial risk; never overwrite an
                # explicitly-supplied r_multiple.
                if (merged_dict.get("realized_pnl") is not None
                        and merged_dict.get("r_multiple") is None):
                    rm = _r_multiple(merged_dict)
                    if rm is not None:
                        merged_dict["r_multiple"] = rm
                # Classify the WIN on TRUE realized (final close + partial banks) so a trade that
                # banked a winning slice but whose runner closed sub-zero isn't mislabeled a loss
                # (cy78 review). Only override when banks exist; a plain close keeps caller's value.
                if (merged_dict.get("realized_pnl") is not None
                        and merged_dict.get("partial_banks")):
                    merged_dict["prediction_correct"] = realized_total(merged_dict) > 0
                # validate the merged record so outcome types are coerced (e.g. datetimes)
                merged = Decision.model_validate(merged_dict)
                r.clear()
                r.update(json.loads(merged.model_dump_json()))
                hit = True
                break  # ids are unique; stop scanning this file
        if hit:
            f.write_text("".join(json.dumps(r) + "\n" for r in records))
            return True
    return False


def append_partial_bank(memory_dir, decision_id: str, bank: dict) -> bool:
    """APPEND a partial scale-out bank to its parent decision's `partial_banks` list (cy78 fix) so
    the slice's realized cash is captured rather than lost — the runner's final close still patches
    `realized_pnl` separately. `bank` carries {pnl, fees, funding, fraction, price, ts}. Returns
    False if the id is not found. Mirrors patch_outcome's monthly-file rewrite."""
    for f in _all_files(memory_dir):
        records = [json.loads(line) for line in f.read_text().splitlines() if line.strip()]
        hit = False
        for r in records:
            if r.get("id") == decision_id:
                banks = list(r.get("partial_banks") or [])
                banks.append(json.loads(json.dumps(bank, default=str)))   # JSON-safe (ts etc.)
                merged = Decision.model_validate({**r, "partial_banks": banks})
                r.clear()
                r.update(json.loads(merged.model_dump_json()))
                hit = True
                break
        if hit:
            f.write_text("".join(json.dumps(r) + "\n" for r in records))
            return True
    return False


def _r_multiple(decision: dict) -> float | None:
    """R-multiple of a closed trade = TRUE realized (final close + partial banks) / initial risk
    (size * |entry - stop|). None when geometry is missing/degenerate."""
    try:
        size = float(decision.get("size") or 0.0)
        risk_per_unit = abs(float(decision["entry"]) - float(decision["stop"]))
        denom = size * risk_per_unit
        if denom <= 0:
            return None
        return realized_total(decision) / denom
    except (TypeError, ValueError, KeyError):
        return None


def realized_total(decision: dict) -> float:
    """The TRUE realized PnL of a (possibly scaled-out) trade: the final-close `realized_pnl` PLUS
    every partial-bank slice. Reading `realized_pnl` alone understates any trade that was scaled out
    (cy22 SOL was 44% invisible). Safe on raw dicts; absent/None fields contribute 0."""
    total = decision.get("realized_pnl") or 0.0
    for b in (decision.get("partial_banks") or []):
        try:
            total += float(b.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
    return total
