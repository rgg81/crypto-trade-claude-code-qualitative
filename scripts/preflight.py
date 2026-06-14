"""Phase 0-2 preflight for the Operation ORACLE sentiment desk (LEAN — NO TA regime classification,
NO analyst funnel).

The 4h decision-loop opener: it reconciles the book against the latest COMPLETED bar, closes any
stop/TP/liquidation hit (paper), prices the working universe (spec + mark + ATR + funding), and
assembles the desk SCORECARD (health, returns, Sharpe/hit-rate/profit-factor, per-expert hit-rates)
plus the month-to-date PACING directive (1%/mo FLOOR — the desk presses ABOVE it). It writes one
self-contained ``state/cycle/<N>/context.json`` the rest of the loop (evidence -> desk -> gate)
reads.

    uv run python scripts/preflight.py --cycle N --symbols BTC/USDT:USDT,ETH/USDT:USDT

Unlike the legacy quant preflight this build sources NO TA regime, NO derivatives briefs, and NO
analyst pass — the directional read is the qualitative sentiment funnel downstream. Price here is
RISK PLUMBING ONLY: mark + ATR for stop placement / sizing / mark-to-market, never a signal.

Market data is served by an INJECTED exchange-like object (``exchange.ohlcv/mark_price/funding/
symbol_spec``) and an injected ``now`` so the whole step runs OFFLINE in tests — no live network.
It is FAIL-SOFT per symbol: a price/spec/funding fetch that raises for one symbol degrades that one
card (``error`` field) and NEVER aborts the cycle; every other symbol and the close path proceed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta

import pandas as pd

from futures_fund.config import Settings, load_settings
from futures_fund.costs import count_funding_events
from futures_fund.cycle_io import save_output
from futures_fund.equity_log import period_return, returns_series
from futures_fund.exits import detect_exit
from futures_fund.journal import (
    patch_outcome,
    read_all_decisions,
    read_open_decisions,
    realized_total,
)
from futures_fund.metrics import agent_attribution, hit_rate, profit_factor, sharpe
from futures_fund.models import PortfolioHealth
from futures_fund.pacing import pacing_state
from futures_fund.portfolio_risk import position_risk
from futures_fund.state import (
    AccountState,
    Position,
    load_account,
    load_positions,
    save_account,
    save_positions,
)

_STATE_DIR = "state"
_MEMORY_DIR = "memory"
_ATR_PERIOD = 14
# Paper exit slippage in bps (mirrors the executor/cycle convention — exit fills are gap-honest in
# detect_exit; this is the per-fill micro-slippage on top).
_SLIPPAGE_BPS = 2.0
# 1%/mo is the FLOOR the desk must clear; pacing presses ABOVE it (anti-martingale, advisory-only).
_MONTHLY_TARGET = 0.01
_TF_SECONDS = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
_TF_HOURS = {"15m": 0.25, "1h": 1.0, "4h": 4.0, "1d": 24.0}


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def last_completed_frame(df: pd.DataFrame | None, now: datetime | None,
                         timeframe: str = "4h") -> pd.DataFrame | None:
    """Drop the still-FORMING last candle so exit/trigger evaluation reads the last COMPLETED bar —
    not a transient intra-candle print (the cy77 fix, inlined here because the legacy `brief` module
    is dropped). If `now` falls inside the last row's window, that row is dropped; an already-closed
    last candle (or no `now`) is left untouched; a single-row frame is never emptied. FAIL-SOFT:
    never raises over bar housekeeping."""
    if df is None or not len(df) or now is None or len(df) < 2:
        return df
    try:
        secs = _TF_SECONDS.get(timeframe, 14400)
        ts = df["timestamp"].iloc[-1]
        ts = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        if (now - ts).total_seconds() < secs:
            return df.iloc[:-1]
    except Exception:  # noqa: BLE001 — never break the cycle over bar housekeeping
        pass
    return df


def _atr_from_ohlcv(df: pd.DataFrame, period: int = _ATR_PERIOD) -> float:
    """House ATR = rolling mean of true range over `period` candles (the desk's standard, matching
    evidence_cli). 0.0 when there are too few candles; the caller treats non-positive/NaN as
    unavailable."""
    if df is None or len(df) < 2:
        return 0.0
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    val = float(atr)
    return val if val == val and val > 0 else 0.0  # val != val filters NaN


# ----------------------------------------------------------------------------- portfolio helpers
# (the legacy `portfolio` module is DROPPED — these tiny pure helpers are inlined; the risk MATH is
# unchanged, reusing position_risk.)


def _unrealized_pnl(p: Position, mark: float) -> float:
    return p.qty * (mark - p.entry) if p.direction == "long" else p.qty * (p.entry - mark)


def _total_equity(balance: float, positions: list[Position],
                  prices: dict[str, float]) -> float:
    """Wallet balance + unrealized PnL of open positions (skips positions with no price)."""
    upnl = sum(_unrealized_pnl(p, prices[p.symbol]) for p in positions if p.symbol in prices)
    return balance + upnl


def _open_heat(positions: list[Position], equity: float) -> float:
    """Sum of per-position stop-out DOWNSIDE risk as a fraction of equity (reuses position_risk)."""
    if equity <= 0:
        return 0.0
    return sum(position_risk(p.qty, p.entry, p.stop, equity, p.direction) for p in positions)


def _recent_hit_rate(memory_dir) -> float:
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    return hit_rate(closed[-30:]) if closed else 0.5


# --------------------------------------------------------------------------------- exit auditing


def audit_exits(exchange, positions: list[Position], account: AccountState, memory_dir,
                now: datetime, timeframe: str) -> tuple[list[Position], list[dict]]:
    """Phase 1: close any position whose latest COMPLETED bar hit stop/TP/liq (paper). Mutates
    `account.balance`, returns the still-open positions and a list of close records, and
    `patch_outcome`s each close into the journal. Mirrors the deterministic engine's audit path
    (detect_exit on the last completed bar, gap-honest fills, per-symbol funding events) WITHOUT the
    dropped TA/brief machinery. FAIL-SOFT per symbol: a market-data error carries the position."""
    still_open: list[Position] = []
    closed: list[dict] = []
    for p in positions:
        # Positions store the RAW exchange id (e.g. BTCUSDT); the exchange/market-data stack speaks
        # UNIFIED ccxt symbols (BTC/USDT:USDT). Resolve raw -> unified before any data call so a
        # carried symbol is never stranded. An unmappable id carries the position untouched.
        unify = getattr(exchange, "unified_for_raw", None)
        unified = unify(p.symbol) if unify else p.symbol
        if unified is None:
            still_open.append(p)
            closed.append({"symbol": p.symbol, "carried": True, "error": "unmappable symbol"})
            continue
        try:
            df = last_completed_frame(exchange.ohlcv(unified, timeframe=timeframe), now, timeframe)
            bar = df.iloc[-1]
            fr = exchange.funding(unified)
            n_events = count_funding_events(p.opened_ts, now, int(fr.interval_hours))
            ct = detect_exit(
                p, bar_high=float(bar["high"]), bar_low=float(bar["low"]),
                bar_open=float(bar["open"]),  # gap-honest stop/liq exit fills
                funding_rate=fr.current_rate, funding_events=n_events,
                slippage_bps=_SLIPPAGE_BPS,
            )
        except Exception as exc:  # noqa: BLE001 — a data error must never strand the audit
            still_open.append(p)
            closed.append({"symbol": p.symbol, "carried": True, "error": str(exc)})
            continue
        if ct is None:
            still_open.append(p)
            continue
        account.balance += ct.realized_pnl
        closed.append({"symbol": p.symbol, "reason": ct.reason, "pnl": round(ct.realized_pnl, 4),
                       "exit_price": ct.exit_price, "decision_id": p.decision_id})
        if p.decision_id:
            patch_outcome(memory_dir, p.decision_id, {
                "exit_ts": now, "realized_pnl": ct.realized_pnl, "fees": ct.exit_fee,
                "funding_paid": ct.funding, "slippage": ct.slippage,
                "prediction_correct": ct.realized_pnl > 0,
            })
    return still_open, closed


# ---------------------------------------------------------------------------------- market cards


def _symbol_card(exchange, symbol: str, timeframe: str, now: datetime) -> dict:
    """Per-symbol RISK-PLUMBING card: SymbolSpec (tick/step/min-notional/MMR) + mark + house ATR +
    current funding rate & interval hours. FAIL-SOFT: any fetch error degrades to a card carrying
    an `error` field (spec/mark/atr/funding None) — never a raise into the universe loop."""
    try:
        spec = exchange.symbol_spec(symbol)
        df = last_completed_frame(exchange.ohlcv(symbol, timeframe=timeframe), now, timeframe)
        atr = _atr_from_ohlcv(df)
        try:
            mark = float(exchange.mark_price(symbol))
        except Exception:  # noqa: BLE001 — fall back to the last completed close
            mark = float(df["close"].iloc[-1]) if df is not None and len(df) else None
        fr = exchange.funding(symbol)
        return {
            "spec": json.loads(spec.model_dump_json()),
            "mark": mark,
            "atr": atr if atr > 0 else None,
            "funding_rate": float(fr.current_rate),
            "funding_interval_hours": float(fr.interval_hours),
        }
    except Exception as exc:  # noqa: BLE001 — one symbol's data error never aborts the cycle
        return {"spec": None, "mark": None, "atr": None, "funding_rate": None,
                "funding_interval_hours": None, "error": str(exc)}


def _marks_from_cards(cards: dict[str, dict], positions: list[Position]) -> dict[str, float]:
    """Mark map keyed by the position SYMBOL (raw exchange id). A symbol card is keyed by the unified
    universe symbol; map each held position's mark via its spec.symbol (raw id) match."""
    by_raw: dict[str, float] = {}
    for card in cards.values():
        spec = card.get("spec")
        mark = card.get("mark")
        if spec and mark is not None:
            by_raw[spec["symbol"]] = float(mark)
    return {p.symbol: by_raw[p.symbol] for p in positions if p.symbol in by_raw}


# ---------------------------------------------------------------------------------- holding cards


def _holding_card(p: Position, mark: float | None, now: datetime, timeframe: str,
                  decision: dict | None) -> dict:
    """The position card the desk reads to decide HOLD vs CLOSE: mark, unrealized %, R-progress,
    bars held, distance to stop/liq, and the ORIGINAL thesis + falsifiable prediction the trade was
    opened on (from the journal). r_progress anchors to the journaled ORIGINAL stop so a trailed
    winner's denominator doesn't collapse/flip sign."""
    sign = 1.0 if p.direction == "long" else -1.0
    original_stop = None
    if decision:
        try:
            s = decision.get("stop")
            original_stop = float(s) if s is not None else None
        except (TypeError, ValueError):
            original_stop = None
    denom_stop = original_stop if original_stop is not None else p.stop
    risk_per_unit = abs(p.entry - denom_stop) or 1e-9
    tf = _TF_HOURS.get(timeframe, 4.0)
    bars_held = (now - p.opened_ts).total_seconds() / 3600.0 / tf
    is_risk_bearing = (p.direction == "long" and p.stop < p.entry) or \
                      (p.direction == "short" and p.stop > p.entry)
    card = {
        "symbol": p.symbol, "direction": p.direction, "qty": p.qty, "entry": p.entry,
        "stop": p.stop, "take_profits": p.take_profits, "liq_price": p.liq_price,
        "leverage": p.leverage, "margin": p.margin,
        "mark": mark, "at_risk": is_risk_bearing,
        "opened_cycle": p.opened_cycle, "decision_id": p.decision_id,
        "bars_held": round(bars_held, 1),
    }
    if mark is not None:
        card["unrealized_pnl_pct"] = round(sign * (mark - p.entry) / p.entry, 4) if p.entry else None
        card["r_progress"] = round(sign * (mark - p.entry) / risk_per_unit, 2)
        card["dist_to_stop_pct"] = round(abs(mark - p.stop) / mark, 4) if mark else None
        card["dist_to_liq_pct"] = round(abs(p.liq_price - mark) / mark, 4) if mark else None
    else:
        card["unrealized_pnl_pct"] = card["r_progress"] = None
        card["dist_to_stop_pct"] = card["dist_to_liq_pct"] = None
    if decision:
        card["original_thesis"] = decision.get("rationale") or decision.get("thesis")
        card["falsifiable_prediction"] = decision.get("falsifiable_prediction")
        card["confidence_at_entry"] = decision.get("confidence")
    return card


# ------------------------------------------------------------------------------------- scorecard


def build_scorecard(state_dir, memory_dir, account: AccountState, health: PortfolioHealth,
                    now: datetime) -> dict:
    """The desk SCORECARD: equity/peak/drawdown/health tier, daily/weekly/monthly PnL %, and the
    realized-performance panel (Sharpe / hit-rate / profit-factor over the journal's CLOSED trades,
    plus per-expert hit-rates from contributing-agents attribution)."""
    closed = [d for d in read_all_decisions(memory_dir) if d.get("realized_pnl") is not None]
    rets = returns_series(state_dir)
    attribution = agent_attribution(closed)
    per_expert = {a: {"hit_rate": round(rec["hit_rate"], 4), "count": rec["count"],
                      "pnl": round(rec["pnl"], 4)}
                  for a, rec in attribution.items()}
    return {
        "equity": round(health.equity, 4),
        "peak_equity": round(health.peak_equity, 4),
        "balance": round(account.balance, 4),
        "drawdown": round(health.drawdown_from_peak, 4),
        "health_tier": health.tier,
        "open_heat": round(health.open_heat, 4),
        "daily_pnl_pct": round(period_return(state_dir, now, 1.0), 4),
        "weekly_pnl_pct": round(period_return(state_dir, now, 7.0), 4),
        "monthly_pnl_pct": round(period_return(state_dir, now, 30.0), 4),
        "closed_trades": len(closed),
        "sharpe": round(sharpe(rets), 4),
        "hit_rate": round(hit_rate(closed), 4),
        "profit_factor": (round(pf, 4) if (pf := profit_factor(closed)) != float("inf") else None),
        "realized_pnl_total": round(sum(realized_total(d) for d in closed), 4),
        "per_expert_hit_rate": per_expert,
    }


# ------------------------------------------------------------------------------------- top-level


def run_preflight(exchange, state_dir, memory_dir, symbols: list[str], cycle: int,
                  now: datetime, *, timeframe: str = "4h", default_balance: float = 10_000.0,
                  monthly_target: float = _MONTHLY_TARGET) -> dict:
    """Run the full preflight and persist ``state/cycle/<cycle>/context.json``; returns the context
    dict. Pure of argparse/config so tests drive it directly with an injected exchange + now."""
    account = load_account(state_dir, default_balance)
    positions = load_positions(state_dir)

    # Phase 1 — close stop/TP/liq hits on the latest completed bar (BEFORE building the context, so
    # the scorecard/holdings reflect the post-audit book). Paper: realized PnL hits the balance.
    positions, closed = audit_exits(exchange, positions, account, memory_dir, now, timeframe)

    # Phase 2 — price the universe: spec + mark + ATR + funding per symbol (risk plumbing only).
    cards = {sym: _symbol_card(exchange, sym, timeframe, now) for sym in symbols}

    # Mark-to-market the surviving book off this cycle's marks, then health/peak.
    prices = _marks_from_cards(cards, positions)
    equity = _total_equity(account.balance, positions, prices)
    peak = max(account.peak_equity, equity)
    health = PortfolioHealth(
        equity=equity, peak_equity=peak,
        open_heat=_open_heat(positions, equity) if equity > 0 else 0.0,
        recent_hit_rate=_recent_hit_rate(memory_dir),
    )

    # Persist the post-audit book + raised high-water mark BEFORE building the context so a crash
    # mid-context never loses a realized close (atomic writes via the state pattern).
    account.peak_equity = peak
    account.updated_ts = now
    save_account(state_dir, account)
    save_positions(state_dir, positions)

    scorecard = build_scorecard(state_dir, memory_dir, account, health, now)

    # Month-to-date pacing keyed to the 1%/mo FLOOR — advisory/utilization-only, anti-martingale
    # (a drawdown never presses); fail-safe -> SOFT on any error.
    try:
        ps = pacing_state(state_dir, now, health, monthly_target=monthly_target)
        pacing = {"mode": ps.mode, "appetite": ps.appetite,
                  "suggested_risk_mult": ps.suggested_risk_mult, "mtd_return": ps.mtd_return,
                  "pace": ps.pace, "pace_gap": ps.pace_gap, "in_drawdown": ps.in_drawdown,
                  "monthly_target": monthly_target, "directive": ps.directive}
    except Exception:  # noqa: BLE001 — pacing is advisory; never break the cycle
        pacing = {"mode": "soft", "monthly_target": monthly_target,
                  "suggested_risk_mult": 0.5,
                  "directive": "SOFT — pacing unavailable; trade conservatively."}

    # Holding cards: HOLD/CLOSE review for each carried position (original thesis from the journal).
    decisions_by_id = {d.get("id"): d for d in read_open_decisions(memory_dir)}
    holdings = [_holding_card(p, prices.get(p.symbol), now, timeframe,
                              decisions_by_id.get(p.decision_id))
                for p in positions]

    context = {
        "cycle": cycle,
        "now": now.isoformat(),
        "equity": round(health.equity, 4),
        "peak_equity": round(health.peak_equity, 4),
        "drawdown": round(health.drawdown_from_peak, 4),
        "health_tier": health.tier,
        "daily_pnl_pct": scorecard["daily_pnl_pct"],
        "weekly_pnl_pct": scorecard["weekly_pnl_pct"],
        "monthly_pnl_pct": scorecard["monthly_pnl_pct"],
        "pacing": pacing,
        "scorecard": scorecard,
        "audit": {"closed": [c for c in closed if not c.get("carried")],
                  "carried_errors": [c for c in closed if c.get("carried")]},
        "symbols": cards,
        "holdings": holdings,
    }
    save_output(state_dir, cycle, "context", context)
    return context


def _build_exchange(settings: Settings):
    """Construct the PAPER (keyless public) exchange from settings. Live env only — tests inject a
    fake exchange and never reach here."""
    from futures_fund.exchange import FuturesExchange
    return FuturesExchange.from_settings(settings)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Lean 4h sentiment-desk preflight (no TA/analyst).")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--symbols", default=None,
                    help="comma-separated unified symbols (the universe). Overrides config.")
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--now", default=None, help="ISO timestamp (UTC); defaults to now")
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    if args.symbols is not None:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = list(settings.symbols)

    exchange = _build_exchange(settings)
    context = run_preflight(
        exchange, args.state_dir, args.memory_dir, syms, args.cycle, _parse_now(args.now),
        timeframe=settings.timeframe, default_balance=settings.account_size_usdt,
    )
    print(json.dumps(context, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
