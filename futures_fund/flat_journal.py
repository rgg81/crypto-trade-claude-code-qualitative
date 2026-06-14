"""Journal of FLAT / declined-setup verdicts.

The desk only ever journaled OPENED trades, so reflection could only mine a winners-vs-losers
contrast — structurally producing risk-reducing ('don't') lessons only. To learn whether standing
aside HELPS or COSTS, we must also persist the trades the desk DECLINED, flagged by whether they
matched its proven edge, then later evaluate how price actually moved. That closes the feedback
loop so the corpus can mint enabling ('DO take it when X') lessons too.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path


def _store(memory_dir) -> Path:
    return Path(memory_dir) / "flat-decisions.jsonl"


def coerce_flat_verdicts(data) -> list[dict]:
    """Normalize the orchestrator's flat_verdicts.json payload to a list of verdict dicts.

    The documented contract is a BARE LIST, but the orchestrator can easily mis-wrap it as
    {"flat_verdicts": [...]} (or {"verdicts": [...]}) — which previously crashed the journal
    stage with a 'str object is not a mapping' error (iterating dict keys). Tolerate both shapes,
    and fail SAFE (empty list) on anything un-coercible, so a single mis-shape never aborts a
    cycle's why-flat accountability. Mirrors the self-healing normalize_reports contract."""
    if isinstance(data, list):
        return [v for v in data if isinstance(v, dict)]
    if isinstance(data, dict):
        for key in ("flat_verdicts", "verdicts"):
            inner = data.get(key)
            if isinstance(inner, list):
                return [v for v in inner if isinstance(v, dict)]
    return []


def append_flat_decision(memory_dir, fields: dict, ts: datetime) -> str:
    """Record a FLAT verdict. Expected fields: cycle, symbol, regime, rating, reason,
    edge_aligned (bool — did it match the crowded-short squeeze-long edge?), favored_side
    ('long'|'short' — the direction the passed-on setup leaned), mark (price at decision).
    Outcome fields (evaluated, favored_move_pct, flat_cost_us) are patched later."""
    data = {**fields, "ts": ts.isoformat() if hasattr(ts, "isoformat") else ts}
    data.setdefault("id", uuid.uuid4().hex)
    data.setdefault("evaluated", False)
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps(data, default=str) + "\n")
    return data["id"]


def read_flat_decisions(memory_dir) -> list[dict]:
    p = _store(memory_dir)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _write_all(memory_dir, rows: list[dict]) -> None:
    _store(memory_dir).write_text("".join(json.dumps(r, default=str) + "\n" for r in rows))


def patch_flat_outcome(memory_dir, fid: str, fields: dict) -> bool:
    rows = read_flat_decisions(memory_dir)
    hit = False
    for r in rows:
        if r.get("id") == fid:
            r.update(fields)
            hit = True
    if hit:
        _write_all(memory_dir, rows)
    return hit


def evaluate_pending_flats(memory_dir, marks: dict[str, float], now: datetime,
                           *, now_cycle: int | None = None, eval_after_cycles: int = 6,
                           min_move: float = 0.02) -> int:
    """Score un-evaluated, edge-aligned FLATs by how price moved in the setup's FAVORED direction —
    but over a MULTI-DAY horizon, not the next-candle bounce. Two mechanics fix the short-horizon
    artifact that kept 'vindicating' the holds on 1-cycle noise:

    1. HORIZON GATING — a decision FINALIZES (`evaluated=True`) only once it is `eval_after_cycles`
       cycles old (≈24h on the 4h cadence, a multi-day window). Before that it stays pending, so a
       single-candle bounce can never lock the verdict. (Falls back to immediate single-shot eval
       when `now_cycle` is unknown — preserves the legacy call/tests.)
    2. MAX FAVORABLE EXCURSION — while pending, each call advances a running `max_favored_move`, so
       a declined trade that trends our way then ROUND-TRIPS still registers the move it would have
       captured (the desk's trades carry take-profits; they don't sit through a full round-trip).
       `favored_move_pct` (and `flat_cost_us`) use that peak; `endpoint_move_pct` keeps the last
       mark for transparency.

    `flat_cost_us` = the peak favorable excursion over the window >= min_move (standing aside cost
    us). Only edge-aligned flats are evaluated. Returns the number NEWLY FINALIZED this call."""
    rows = read_flat_decisions(memory_dir)
    n = 0
    dirty = False
    for r in rows:
        if r.get("evaluated") or not r.get("edge_aligned"):
            continue
        m0, sym = r.get("mark"), r.get("symbol")
        m1 = marks.get(sym)
        dcyc = r.get("cycle")
        overdue = (now_cycle is not None and dcyc is not None
                   and (now_cycle - dcyc) >= eval_after_cycles)
        if not m0 or m1 is None:
            # Un-repriceable this cycle (symbol dropped from the universe -> absent from marks).
            # Don't let it REPLAY its stale snapshot forever (the cy37 ZEC bug, 41 cycles): once
            # well past the horizon, FINALIZE on the last running peak (None peak -> never moved ->
            # no cost, so it can't wrongly indict the conservatism). Recent ones stay pending.
            if overdue and m0:
                mm = r.get("max_favored_move")
                r.update({"evaluated": True, "eval_mark": None, "stale_unrepriced": True,
                          "favored_move_pct": mm,
                          "flat_cost_us": (mm is not None and mm >= min_move)})
                n += 1
                dirty = True
            continue
        side = r.get("favored_side", "long")
        move = (m1 - m0) / m0 * (1.0 if side == "long" else -1.0)
        prev_max = r.get("max_favored_move")
        max_move = move if prev_max is None else max(prev_max, move)
        ready = now_cycle is None or dcyc is None or overdue
        if ready:
            r.update({"evaluated": True, "eval_mark": m1,
                      "eval_ts": now.isoformat() if hasattr(now, "isoformat") else now,
                      "max_favored_move": max_move, "endpoint_move_pct": move,
                      "favored_move_pct": max_move, "flat_cost_us": max_move >= min_move})
            n += 1
            dirty = True
        elif max_move != prev_max:           # still pending — just advance the running peak
            r["max_favored_move"] = max_move
            dirty = True
    if dirty:
        _write_all(memory_dir, rows)
    return n
