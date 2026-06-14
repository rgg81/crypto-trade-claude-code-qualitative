"""Conditional / trigger orders — resting intents that let the desk act on its own analysis
across cycles instead of "wait and re-decide by hand" (which lost the whole SUI move: it fell
straight down, never bouncing to the 0.887 trigger that only lived in the orchestrator's head).

A trigger fires off the latest COMPLETED 4h bar (hybrid by kind: stop-entry on a CLOSE beyond the
level = a confirmed break; limit-entry on a LOW/HIGH TOUCH = a pullback fill), then becomes a
NORMAL proposal at the trigger price and routes through the EXACT existing gate (RR>=2, heat cap,
1%-sizing, liq) re-checked against the LIVE regime — no privileged path. A FIRED trigger is already
a confirmed break, so it is exempt from the gate's counter-regime confirmation transform.
"""
from __future__ import annotations

import json
import math
import os
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

# A require_oi_rising stop_entry fires on its price-break ONLY IF reactive OI growth exceeds this
# deadband. +0.5% (not strict >0) so flat/noise OI does NOT count as 'rising' fuel.
OI_RISING_EPS = 0.005

# Stale-trigger geometry deadband. A stop_entry's swing anchor (swing_low for a breakdown short,
# swing_high for a breakout long) must move PAST its trigger_level by more than `buffer` before the
# trigger is judged geometrically STALE, where buffer = max(ATR_FRAC * atr, PCT_FALLBACK * |level|).
# 0.25 grounded in the cy43 ETH inversion (the swing crossed the level by 0.44*ATR there, so 0.25
# catches it with margin while staying well above tick wobble; 0.5 would have MISSED it). The pct
# floor gives a finite deadband when atr is missing/zero. Symmetric across long/short.
STALE_TRIGGER_ATR_FRAC = 0.25
STALE_TRIGGER_PCT_FALLBACK = 0.0025


class PendingOrder(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str                      # RAW exchange id (BTCUSDT), matching AgentProposal/Position
    direction: str                   # 'long' | 'short'
    kind: str                        # 'stop_entry' | 'limit_entry'
    trigger_level: float
    stop: float
    take_profits: list[float] = Field(default_factory=list)
    atr: float = 0.0
    falsifiable_prediction: str = ""
    rationale: str = ""
    confidence: float = 0.5
    risk_mult: float = 1.0            # optional per-trade risk REDUCTION; gate clamps to (0,1]
    # OPT-IN OI-confirmation: when True, this stop_entry may fire on its price-break ONLY IF OI is
    # rising at fire time (fresh fuel confirming the break); a spent-OI break is a bounce-trap and
    # HOLDS the trigger armed. Default False = today's behavior (OI never consulted). Symmetric:
    # applied identically to a flush-SHORT down-break and a squeeze-LONG up-break.
    require_oi_rising: bool = False
    # The directional swing captured at ARM time (swing_low for a short breakdown, swing_high for a
    # long breakout), so a later cycle can detect the swing crossing PAST this level and auto-cancel
    # the stale trigger. None = unstamped (legacy / non-breakdown trigger) -> NEVER auto-revalidated
    # (fail-safe; auto-cancel only ever acts on a confirmed arm->now crossing of a real anchor).
    anchor_swing: float | None = None
    created_cycle: int = 0
    expires_cycle: int = 0
    # GAP-HONEST FILL PRICE stamped at fire time (a stop_entry fills at trigger_level only if price
    # TRADED there this bar; on a clean gap PAST the level the first live print is the bar open —
    # worse). None = not yet fired / in-range / no open available -> fired_to_proposal falls back to
    # trigger_level. Transient (set on the in-memory fired order; never persisted to the store).
    fire_fill: float | None = None


def _store(state_dir) -> Path:
    return Path(state_dir) / "pending_orders.json"


def load_pending_orders(state_dir) -> list[PendingOrder]:
    """Missing file -> []. Skips per-order malformed records; never raises (corrupt store ==
    no armed triggers, fail-safe)."""
    p = _store(state_dir)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return []
    out = []
    for rec in raw if isinstance(raw, list) else []:
        try:
            out.append(PendingOrder.model_validate(rec))
        except Exception:  # noqa: BLE001 — drop a malformed order, keep the rest
            continue
    return out


def save_pending_orders(state_dir, orders: list[PendingOrder]) -> None:
    p = _store(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps([o.model_dump(mode="json") for o in orders], indent=2))
    os.replace(tmp, p)


def _key(o: PendingOrder) -> tuple:
    return (o.symbol, o.direction, o.kind)


def upsert_triggers(orders: list[PendingOrder], new_triggers: list[PendingOrder]) -> list[PendingOrder]:
    """Append-or-REPLACE by (symbol, direction, kind); dedupe the new batch among itself (last
    wins) so a re-stated trigger never duplicates."""
    merged = {_key(o): o for o in orders}
    for nt in new_triggers:
        merged[_key(nt)] = nt
    return list(merged.values())


def fired_to_proposal(o: PendingOrder) -> dict:
    """A fired trigger becomes a normal AgentProposal at the GAP-HONEST fill price: the trigger
    level when price TRADED through it this bar (a resting stop fills at its level), or the bar OPEN
    when the bar gapped clean past the level (the first live print — worse, never better). It then
    competes in the same gate (RR/heat/sizing/liq) as fresh opens — but, being an already confirmed
    break, it is EXEMPT from the counter-regime confirmation transform (not re-armed). The realized
    open also carries the executor's adverse slippage (fill = trigger ± slip)."""
    entry = o.fire_fill if o.fire_fill is not None else o.trigger_level
    return {"symbol": o.symbol, "direction": o.direction, "entry": entry,
            "stop": o.stop, "take_profits": o.take_profits, "atr": o.atr,
            "confidence": o.confidence, "risk_mult": o.risk_mult,
            "falsifiable_prediction": o.falsifiable_prediction,
            "rationale": f"[trigger:{o.kind}] {o.rationale}"}


def _wrong_side_stop(o: PendingOrder) -> bool:
    # a long's stop must be BELOW the entry/trigger; a short's ABOVE. Inverted => reject.
    return (o.direction == "long" and o.stop >= o.trigger_level) or \
           (o.direction == "short" and o.stop <= o.trigger_level)


def stop_entry_wrong_side_of_mark(o: PendingOrder, mark) -> bool:
    """A breakout/breakdown stop_entry must have ROOM TO BREAK from the current mark: a SHORT
    breakdown trigger sits BELOW the mark, a LONG breakout trigger ABOVE it. A stop_entry placed on
    the WRONG side (short at/above mark, long at/below) has 'already broken' — it would fire on the
    next bar's close with no genuine break (the cy80 BNB @611 fired off a 603.79 close because it
    was armed below the mark). Reject those at ARM time. limit_entry is EXEMPT — a limit rests on
    the far side by design (short above / long below). Fail-safe: a missing/non-finite mark -> not
    wrong-side (can't validate -> keep)."""
    if o.kind != "stop_entry":
        return False
    try:
        m = float(mark)
        if not math.isfinite(m):
            return False
    except (TypeError, ValueError):
        return False
    return (o.direction == "short" and o.trigger_level >= m) or \
           (o.direction == "long" and o.trigger_level <= m)


def _oi_confirms(oi_change_by_symbol, symbol: str) -> bool:
    """OI-confirmation predicate for require_oi_rising triggers: True ONLY if fresh OI is RISING
    (> OI_RISING_EPS) for `symbol`. Missing / None / NaN -> False (FAIL-SAFE: the break HOLDS the
    trigger armed, never a spurious fire). Direction-AGNOSTIC — one predicate applied identically to
    long and short, so the OI-gate cannot introduce a long/short bias (market-neutral mandate)."""
    oi = (oi_change_by_symbol or {}).get(symbol)
    return oi is not None and not math.isnan(oi) and oi > OI_RISING_EPS


def _stale_geometry(o: PendingOrder, swing_high, swing_low) -> bool:
    """True iff a stop_entry trigger's swing anchor has CROSSED PAST its level since it was armed
    (the cy43 ETH inversion): a breakdown SHORT must fire at/below support, so it is STALE once the
    swing_low — which was at/above the level at arm — falls below it; a breakout LONG is STALE once
    the swing_high — at/below the level at arm — rises above it. Crossing is judged against a single
    deadband line L (trigger_level −/+ buffer) so a within-noise wobble does NOT trip it.

    Symmetric. ONLY a stop_entry with a recorded `anchor_swing` is revalidated: a limit_entry
    (pullback TOUCH, opposite geometry), a non-long/short order, or any UNSTAMPED trigger (legacy,
    or placed away from the 20-bar swing) is NEVER judged stale — so auto-cancel can only
    retire a trigger that was a genuine swing breakout/breakdown anchor and has since been crossed.
    FAIL-SAFE: a missing / non-finite (None/NaN/inf) anchor, current swing, or trigger_level -> NOT
    stale (keep the trigger armed)."""
    if o.kind != "stop_entry" or o.direction not in ("long", "short"):
        return False
    lvl, anchor0 = o.trigger_level, o.anchor_swing
    if lvl is None or not math.isfinite(lvl) or anchor0 is None or not math.isfinite(anchor0):
        return False
    now_swing = swing_low if o.direction == "short" else swing_high
    if now_swing is None or not math.isfinite(now_swing):
        return False
    atr = o.atr if (o.atr and math.isfinite(o.atr)) else 0.0
    buffer = max(atr * STALE_TRIGGER_ATR_FRAC, abs(lvl) * STALE_TRIGGER_PCT_FALLBACK)
    if o.direction == "short":            # support crossed the L line downward (arm >= L, now < L)
        line = lvl - buffer
        return anchor0 >= line and now_swing < line
    line = lvl + buffer                   # resistance crossed the L line upward (arm <= L, now > L)
    return anchor0 <= line and now_swing > line


def revalidate_triggers(orders: list[PendingOrder],
                        swings_by_symbol: dict) -> tuple[list, list]:
    """Partition armed orders into (stale, healthy) by stop_entry swing geometry. `swings_by_symbol`
    maps RAW symbol -> (swing_high, swing_low). A symbol with NO swing entry (feed gap) is FAIL-SAFE
    kept (healthy). Pure — the caller cancels the stale set through the normal cancel flow (never a
    manual store edit), so a geometrically-inverted trigger neither fires nor persists."""
    stale, healthy = [], []
    for o in orders:
        sh, sl = (swings_by_symbol or {}).get(o.symbol, (None, None))
        (stale if _stale_geometry(o, sh, sl) else healthy).append(o)
    return stale, healthy


def trigger_rr(o: PendingOrder) -> float:
    """Reward:risk a stop_entry will be scored on by the gate AT FIRE TIME:
    |nearest_TP − trigger_level| / |stop − trigger_level|. A fired stop_entry fills at its
    trigger_level (entry == trigger), so this RR is FIXED from arm-time to fire-time: a trigger
    armed below the gate's MIN_RR floor is deterministically doomed to fire-then-veto. Mirrors
    risk_gate._reward_risk (nearest TP to entry). Returns 0.0 on missing TP / zero-or-undefined risk
    (a degenerate trigger reads as below any positive floor). Direction-agnostic."""
    lvl = o.trigger_level
    tps = o.take_profits or []
    if lvl is None or not math.isfinite(lvl) or not tps:
        return 0.0
    if o.stop is None or not math.isfinite(o.stop):
        return 0.0
    risk = abs(o.stop - lvl)
    if risk <= 0:
        return 0.0
    nearest = min(tps, key=lambda tp: abs(tp - lvl))
    return abs(nearest - lvl) / risk


def low_rr_triggers(orders: list[PendingOrder], min_rr: float,
                    eps: float = 1e-6) -> tuple[list, list]:
    """Partition armed orders into (sub_floor, ok) by whether a stop_entry's arm-time RR clears the
    gate's `min_rr` (using the gate's exact `rr < min_rr − eps` condition). ONLY a stop_entry with a
    stop + at least one TP is checked; a limit_entry / missing-data order always passes (the gate
    scores it on fire). A sub-floor stop_entry would only fire then be RR-vetoed (entry == trigger ⇒
    RR fixed), so it is refused at arm-time — a refuse-only guard that never opens/sizes anything.
    Pure and DIRECTION-AGNOSTIC (long/short symmetric)."""
    sub, ok = [], []
    for o in orders:
        checkable = (o.kind == "stop_entry" and o.trigger_level is not None
                     and o.stop is not None and bool(o.take_profits))
        (sub if (checkable and trigger_rr(o) < min_rr - eps) else ok).append(o)
    return sub, ok


def non_crypto_triggers(orders: list[PendingOrder],
                        is_crypto_by_symbol: dict) -> tuple[list, list]:
    """Partition armed orders into (untradeable, tradeable) by whether the symbol is a CRYPTO
    market. The desk is crypto-only: a trigger resting on a now-listed tokenized stock / commodity
    / metal / pre-IPO / index must be retired through the normal gate flow (never a manual store
    edit). `is_crypto_by_symbol` maps RAW symbol -> True (proven crypto) / False (proven non-crypto)
    / None or ABSENT (unknown). FAIL-CLOSED — the deliberate OPPOSITE of revalidate_triggers'
    fail-SAFE-keep: only a value of exactly True is tradeable; False / None / missing all go to
    untradeable (never keep a stock armed on a classification gap). Pure and DIRECTION-AGNOSTIC —
    keys on the symbol alone, never the side, so the crypto-only gate adds no long/short bias."""
    untradeable, tradeable = [], []
    for o in orders:
        ok = (is_crypto_by_symbol or {}).get(o.symbol)
        (tradeable if ok is True else untradeable).append(o)
    return untradeable, tradeable


def _gap_honest_fill(trigger_level, open_, low, high):
    """Realistic fill price for a fired stop_entry. `trigger_level` when price TRADED through the
    level this bar (low <= level <= high -> a live resting stop fills AT its level); otherwise the
    bar OPEN — the first live print when the bar gapped clean PAST the level (worse, never better).
    Missing low/high (range unknown) or missing open -> fall back to trigger_level; never
    raises. Pure and DIRECTION-AGNOSTIC (keys on the level vs the bar range, never the side)."""
    gapped = low is not None and high is not None and not (low <= trigger_level <= high)
    if gapped and open_ is not None and math.isfinite(open_):  # NaN/inf open -> safe fallback
        return float(open_)
    return float(trigger_level)


def check_pending_orders(state_dir, bars_by_symbol: dict, cycle_no: int,
                         held_symbols=frozenset(),
                         oi_change_by_symbol: dict | None = None) -> tuple[list, list, list]:
    """Evaluate every armed order against the latest COMPLETED 4h bar (RAW-keyed). Returns
    (fired, expired, remaining) — disjoint. FIRE precedes EXPIRY. Held-symbol, knife-guarded, and
    wrong-side orders are CONSUMED (in none of the three lists -> removed from the store). No-bar
    orders are UNEVALUABLE and stay in `remaining` (still pending) unless they also expire."""
    fired, expired, remaining = [], [], []
    for o in load_pending_orders(state_dir):
        if o.symbol in held_symbols:
            continue  # no stacking against a live position; the team flips via holdings CLOSE
        bar = bars_by_symbol.get(o.symbol)
        fire = consumed = False
        if bar is not None and not _wrong_side_stop(o):
            close, low, high = bar.get("close"), bar.get("low"), bar.get("high")
            if o.kind == "stop_entry":  # confirmed break on the bar CLOSE
                fire = (o.direction == "short" and close is not None and close < o.trigger_level) or \
                       (o.direction == "long" and close is not None and close > o.trigger_level)
                if fire and o.require_oi_rising:   # symmetric fresh-OI gate (fail-safe)
                    fire = _oi_confirms(oi_change_by_symbol, o.symbol)
                if fire:                           # GAP-HONEST FILL (direction-agnostic, Rule 5):
                    o.fire_fill = _gap_honest_fill(o.trigger_level, bar.get("open"), low, high)
            else:                        # limit_entry: TOUCH of the level
                if o.direction == "long" and low is not None and low <= o.trigger_level:
                    if low <= o.stop:    # knife guard: bar tagged trigger AND stop in one bar
                        consumed = True
                    else:
                        fire = True
                elif o.direction == "short" and high is not None and high >= o.trigger_level:
                    if high >= o.stop:
                        consumed = True
                    else:
                        fire = True
        elif bar is not None and _wrong_side_stop(o):
            consumed = True              # inverted geometry -> drop, never re-arm
        if fire:                          # FIRE wins over expiry
            fired.append(o)
        elif consumed:
            continue                      # knife / wrong-side -> removed
        elif cycle_no >= o.expires_cycle:
            expired.append(o)
        else:
            remaining.append(o)           # unfired (incl. no-bar unevaluable) stays armed
    return fired, expired, remaining
