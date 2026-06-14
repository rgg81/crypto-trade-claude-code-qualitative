"""Phase 9 CLI: the deterministic RISK GATE + CONSOLIDATION + EXECUTION + JOURNAL of the
qualitative sentiment desk. This is the crux — the single authority that turns the Decider's
sentiment proposals into a paper book, gated by the (separate) deterministic Auditor.

    uv run python scripts/gate_execute_cli.py --cycle N

Pipeline (fail-closed, survival-first):
  1. AUDIT GATE FIRST. If ``sentiment_audit.audit_gate_ok(state_dir, N)`` is False (the Auditor
     vetoed, never ran, or its verdict is malformed) NOTHING is opened. Exits/closes and de-risk
     still run; report.json is written with reason "audit veto" and the process exits 0.
  2. Load ``context.json`` (per-symbol spec/mark/atr/funding, crowd_mood, scorecard, pnl pcts),
     ``positions.json`` and ``account.json``.
  3. Map the cycle CrowdMood -> RegimeState via ``models.mood_to_regime`` (per-coin override, else
     the market-level mood, else neutral).
  4. For each AgentProposal: build a TradeProposal, assemble GateInputs, call ``risk_gate``.
     Approved/resized SizedTrades are collected; vetoes are recorded with reasons.
  5. Apply the team's management review (hold/close/reduce) on held positions, and arm/cancel
     resting triggers via ``pending_orders``.
  6. CONSOLIDATE the approved set: gross-heat cap + CVaR de-risk + correlation-cluster scaling.
  7. EXECUTE survivors (paper, slipped fill); update positions/account; journal a Phase-1 decision
     per open carrying contributing_agents, retrieved_memory_ids, falsifiable_prediction, mood.
  8. Write ``state/cycle/<N>/report.json`` with the candle/ran_at run-markers the scheduler reads.

OFFLINE by construction: every price/spec/funding figure comes from the injected ``context.json``
(assembled upstream by the evidence/decider phases), so this step never touches the network. The
exchange is only consulted opportunistically (for live close marks); tests inject a fake one.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund import sentiment_audit
from futures_fund.config import Settings, load_settings
from futures_fund.consolidation import consolidate, cvar_risk_multiplier
from futures_fund.content_store import get_item
from futures_fund.contracts import AgentProposal, SentimentRead, to_trade_proposal
from futures_fund.cycle_io import load_output, save_output
from futures_fund.equity_log import record_equity, returns_series
from futures_fund.executor import close_at_mark, open_position
from futures_fund.journal import append_decision
from futures_fund.models import (
    CrowdMood,
    MmrBracket,
    PortfolioHealth,
    SymbolSpec,
    mood_to_regime,
)
from futures_fund.pending_orders import (
    PendingOrder,
    load_pending_orders,
    non_crypto_triggers,
    save_pending_orders,
    upsert_triggers,
)
from futures_fund.portfolio_risk import portfolio_heat
from futures_fund.risk_gate import GateInputs, evaluate
from futures_fund.rr_floor import effective_rr_floor, load_rr_floor
from futures_fund.scheduling import floor4
from futures_fund.state import (
    Position,
    load_account,
    load_positions,
    save_account,
    save_positions,
)

_STATE_DIR = "state"
_MEMORY_DIR = "memory"
_CONTENT_DIR = "content"
_DEFAULT_SLIPPAGE_BPS = 2.0
# The ungrounded-thesis backstop requires a read this confident to GROUND a held position's
# direction; thinner reads do not keep a stale position alive (mirrors the Decider's discipline
# of not resting a directional verdict on a near-flat lone read).
_GROUND_MIN_CONFIDENCE = 0.4


# --------------------------------------------------------------------------- #
# context.json helpers (all data the gate needs is assembled upstream)        #
# --------------------------------------------------------------------------- #
def _coerce_spec(raw: dict | None, symbol: str) -> SymbolSpec | None:
    """Build a SymbolSpec from a context entry's ``spec`` blob. Returns None when absent/malformed
    so the caller VETOES that proposal (a fabricated/missing spec must never reach the gate)."""
    if not isinstance(raw, dict):
        return None
    try:
        return SymbolSpec.model_validate(raw)
    except Exception:  # noqa: BLE001 — a malformed spec is a veto, never a crash
        return None


def _default_spec(symbol: str) -> SymbolSpec:
    """A conservative fallback spec (single wide MMR bracket) so a context that carries mark/atr but
    no explicit spec can still be sized. min_notional 5.0 mirrors the desk's standard floor."""
    return SymbolSpec(
        symbol=symbol, tick_size=0.01, step_size=0.001, min_notional=5.0,
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12,
                                 mmr=0.005, maint_amount=0.0, max_leverage=125.0)],
    )


def _crowd_mood(raw: dict | None) -> CrowdMood:
    """Parse a {mood, dispersion} blob into a CrowdMood. FAIL-SOFT: anything missing/malformed
    falls back to a neutral, consensus crowd (the desk's safe default)."""
    if isinstance(raw, dict):
        try:
            return CrowdMood.model_validate(raw)
        except Exception:  # noqa: BLE001
            pass
    return CrowdMood(mood="neutral", dispersion=0.0)


def _position_dicts(positions: list[Position]) -> list[dict]:
    """The {qty, entry, stop, direction} rows the gate/consolidation read for open heat."""
    return [{"qty": p.qty, "entry": p.entry, "stop": p.stop, "direction": p.direction}
            for p in positions]


def _cards_by_raw_id(sym_ctx: dict) -> dict[str, dict]:
    """Index the context's per-symbol cards by the RAW exchange id (e.g. BTCUSDT) the Decider's
    proposals carry. The REAL preflight keys ``context['symbols']`` by the UNIFIED ccxt symbol
    (BTC/USDT:USDT) but stamps the raw id inside each card's ``spec.symbol`` — so a lookup by the
    proposal's raw symbol must go through that. FAIL-SOFT: cards keyed by the raw id directly (the
    hand-built test shape) still resolve via the fall-through. The raw id always wins so a real
    unified-keyed context never strands a proposal as 'no spec'."""
    by_raw: dict[str, dict] = {}
    for key, card in sym_ctx.items():
        if not isinstance(card, dict):
            continue
        spec = card.get("spec")
        raw = spec.get("symbol") if isinstance(spec, dict) else None
        # the raw id from the spec blob is authoritative; only fall back to the dict key (which may
        # itself be the raw id in a hand-built context) when the spec carries no symbol.
        by_raw.setdefault(raw or key, card)
        if raw and raw != key:
            by_raw[raw] = card
    return by_raw


def _loss_pcts(context: dict) -> tuple[float, float, float]:
    """Resolve the circuit-breaker's daily/weekly/monthly loss inputs from the context. The REAL
    preflight emits these as TOP-LEVEL keys (daily_pnl_pct/weekly_pnl_pct/monthly_pnl_pct); a legacy
    hand-built context may nest them under a {pnl: {daily_pct, ...}} block. Read the top-level keys
    first (ground truth) and only fall back to the block, so the loss limbs of the breaker fire on
    the producer's real output instead of silently defaulting to 0.0."""
    pnl = context.get("pnl") or {}

    def pick(top_key: str, block_key: str) -> float:
        v = context.get(top_key)
        if v is None:
            v = pnl.get(block_key)
        return float(v or 0.0)

    return (pick("daily_pnl_pct", "daily_pct"),
            pick("weekly_pnl_pct", "weekly_pct"),
            pick("monthly_pnl_pct", "monthly_pct"))


# --------------------------------------------------------------------------- #
# ungrounded-thesis backstop (RISK-REDUCING: only ever CLOSES, never opens)    #
# --------------------------------------------------------------------------- #
# Quote/settlement suffixes a raw exchange symbol wraps its base coin in. Positions carry the raw
# exchange id (XRPUSDT) while SentimentReads carry the bare coin (XRP), so we strip these to recover
# the base coin for matching — mirrors sentiment_audit._coin_of.
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BUSD", "PERP")


def _coin_of(symbol: str) -> str:
    """Map a position's raw exchange symbol to its base coin token (XRPUSDT -> XRP,
    XRP/USDT:USDT -> XRP). Drops any ``/quote`` or ``:settle`` decoration, then a trailing quote
    suffix; falls back to the whole upper-cased token when nothing strips (an already-bare coin)."""
    s = str(symbol).strip().upper()
    s = s.split(":", 1)[0].split("/", 1)[0]  # drop :settle then /quote decoration
    for suffix in _QUOTE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def _load_cycle_reads(state_dir, cycle: int) -> list[SentimentRead] | None:
    """Parse this cycle's ``sentiment_reads.json`` (a ``list[SentimentRead]``). FAIL-SOFT: returns
    None when the file is missing OR malformed (absent file / not a list / any read fails to
    validate) so the backstop SKIPS entirely — it must never crash, and must NEVER auto-close on
    missing/garbage read data (closing on absent evidence is the opposite of risk-reducing)."""
    try:
        raw = load_output(state_dir, cycle, "sentiment_reads")
    except FileNotFoundError:
        return None
    except Exception:  # noqa: BLE001 — a torn/garbage reads file => skip the backstop, never crash
        return None
    if not isinstance(raw, list):
        return None
    try:
        return [SentimentRead.model_validate(r) for r in raw]
    except Exception:  # noqa: BLE001 — a malformed read => skip the backstop, never crash
        return None


def _direction_grounded(
    content_dir, coin: str, direction: str, reads: list[SentimentRead]
) -> bool:
    """GROUNDED iff >= 1 SentimentRead for ``coin`` matches the position ``direction`` with
    confidence >= _GROUND_MIN_CONFIDENCE AND cites >= 1 item_id that RESOLVES in the content store.

    A SHORT is grounded by a BEARISH read (stance "bearish" or s<0); a LONG by a BULLISH read
    (stance "bullish" or s>0). The cited-item resolution reuses ``content_store.get_item`` so a read
    resting only on hallucinated/unresolvable ids does NOT ground a live position (a thesis with no
    surviving evidence is ungrounded). FAIL-SOFT on a torn store: a get_item that raises is treated
    as 'does not resolve'."""
    want = _coin_of(coin).upper()
    for read in reads:
        if _coin_of(read.coin).upper() != want:
            continue
        bearish = read.stance == "bearish" or read.s < 0
        bullish = read.stance == "bullish" or read.s > 0
        matches = bearish if direction == "short" else bullish
        if not matches or read.confidence < _GROUND_MIN_CONFIDENCE:
            continue
        for claim in read.claims:
            for iid in claim.item_ids:
                try:
                    if get_item(content_dir, iid) is not None:
                        return True
                except Exception:  # noqa: BLE001 — torn store => this id doesn't resolve, keep scanning
                    continue
    return False


def _close_ungrounded(
    content_dir, state_dir, cycle: int, now: datetime,
    positions: list[Position], managed_symbols: set[str], cards_by_raw: dict,
    exchange, account, memory_dir, slippage_bps: float,
    closed: list[dict], warnings: list[str],
) -> list[Position]:
    """Final risk-reducing pass: CLOSE any STILL-OPEN held position whose direction is no longer
    supported by a fresh grounded SentimentRead for its coin. Returns the surviving positions.

    Skips (keeps) any position opened in the CURRENT cycle (``opened_cycle == cycle`` — a brand-new
    open is never coasting) and any the Decider already managed this cycle. FAIL-SOFT: a
    missing/malformed reads file skips the whole pass (closes nothing). MONEY-SAFETY: a non-positive
    / missing mark is treated as no price — the close is SKIPPED, the position KEPT, and warned (a
    close at price ~0 would book a fabricated catastrophic loss)."""
    reads = _load_cycle_reads(state_dir, cycle)
    if reads is None:
        return positions  # missing/malformed reads => skip the backstop entirely (close nothing)

    surviving: list[Position] = []
    for pos in positions:
        # never auto-close a position opened THIS cycle, or one the Decider already managed.
        if pos.opened_cycle == cycle or pos.symbol in managed_symbols:
            surviving.append(pos)
            continue
        if _direction_grounded(content_dir, pos.symbol, pos.direction, reads):
            surviving.append(pos)
            continue
        # ungrounded thesis -> close at the context mark, money-safety guard mark>0.
        entry = cards_by_raw.get(pos.symbol) or {}
        mark = entry.get("mark")
        if mark is None and exchange is not None:
            try:
                mark = exchange.mark_price(_unified(pos.symbol))
            except Exception:  # noqa: BLE001 — no mark -> cannot close this cycle (fail-safe keep)
                mark = None
        try:
            mark_val = float(mark) if mark is not None else None
        except (TypeError, ValueError):
            mark_val = None
        if mark_val is None or mark_val <= 0:
            warnings.append(f"{pos.symbol}: auto-close (ungrounded thesis) skipped — no valid mark "
                            f"available (mark={mark!r}); position KEPT")
            surviving.append(pos)
            continue
        funding_rate = float(entry.get("funding_rate", 0.0) or 0.0)
        ct = close_at_mark(pos, mark_val, funding_rate=funding_rate, funding_events=1,
                           slippage_bps=slippage_bps)
        account.balance += ct.realized_pnl
        closed.append({
            "symbol": pos.symbol, "direction": pos.direction,
            "reason": "auto-close: ungrounded thesis (no fresh sentiment read supports direction)",
            "realized_pnl": ct.realized_pnl,
        })
        if pos.decision_id:
            _patch_close(memory_dir, pos.decision_id, ct, now)
    return surviving


# --------------------------------------------------------------------------- #
# core gate + execute step (pure of argparse so tests drive it directly)       #
# --------------------------------------------------------------------------- #
def gate_execute(
    state_dir,
    memory_dir,
    cycle: int,
    now: datetime,
    *,
    settings: Settings,
    exchange=None,
    slippage_bps: float = _DEFAULT_SLIPPAGE_BPS,
    content_dir=_CONTENT_DIR,
) -> dict:
    """Run the full gate -> consolidate -> execute -> journal pipeline for ``cycle`` and return the
    report dict (also persisted to ``state/cycle/<cycle>/report.json``).

    Reads ``proposals.json`` + ``context.json`` from this cycle's dir, ``positions.json`` and
    ``account.json`` from ``state_dir``, and ``auditor.json`` for the fail-closed audit gate.
    Injectable end-to-end (no network): ``exchange`` is only used for discretionary close marks and
    may be None (marks then come from context)."""
    warnings: list[str] = []
    opened: list[dict] = []
    closed: list[dict] = []
    vetoed: list[dict] = []

    account = load_account(state_dir, settings.account_size_usdt)
    positions = load_positions(state_dir)

    # ---- context (per-symbol spec/mark/atr/funding, crowd_mood, pnl, scorecard) -------------
    try:
        context = load_output(state_dir, cycle, "context")
    except FileNotFoundError:
        context = {}
        warnings.append("context.json MISSING — using empty context (no opens possible)")
    sym_ctx: dict = context.get("symbols") or {}
    # The Decider's proposals carry the RAW exchange id (BTCUSDT) but the preflight keys cards by
    # the UNIFIED ccxt symbol (BTC/USDT:USDT); resolve every per-symbol lookup via this raw index.
    cards_by_raw = _cards_by_raw_id(sym_ctx)
    daily_pct, weekly_pct, monthly_pct = _loss_pcts(context)
    scorecard = context.get("scorecard") or {}
    # The REAL preflight scorecard writes `hit_rate`; a legacy hand-built scorecard may carry
    # `recent_hit_rate`. Read the producer's real key first so PortfolioHealth reflects the desk's
    # actual closed-trade hit-rate instead of silently defaulting to 0.5.
    recent_hit_rate = float(
        scorecard.get("hit_rate", scorecard.get("recent_hit_rate", 0.5)) or 0.5)
    market_mood = _crowd_mood(context.get("crowd_mood"))

    # ---- proposals.json (sentiment funnel output) -------------------------------------------
    try:
        payload = load_output(state_dir, cycle, "proposals")
    except FileNotFoundError:
        payload = {}
        warnings.append("proposals.json MISSING — treating as a stand-down (no proposals)")
    raw_proposals = payload.get("proposals") or []
    # A missing/null `management` key must NEVER reach the close path as "close everything"; coerce
    # to an empty review (holdings KEPT) and surface the anomaly (fail-loud, Rule 6).
    if payload.get("management") is None and "management" in payload:
        warnings.append("proposals.json `management` is null — treating as empty (holdings KEPT)")
    management = payload.get("management") or []
    new_triggers_raw = payload.get("triggers") or []
    cancel_triggers = payload.get("cancel_triggers") or []

    # ---- FAIL-CLOSED AUDIT GATE: decide BEFORE any open whether new entries are allowed ------
    audit_ok = sentiment_audit.audit_gate_ok(state_dir, cycle)
    audit_vetoed = not audit_ok
    if audit_vetoed:
        warnings.append("AUDIT VETO — auditor.json absent/failed/malformed; NO new positions, "
                        "exits + de-risk only")
    # PER-PROPOSAL SCOPING: even when the gate passes (no fatal), the Auditor may BLOCK specific
    # proposals whose evidence is too thin/inconsistent/degraded to act on. Those symbols are skipped
    # (recorded under `vetoed` as "audit-blocked"), but the clean proposals still open. Read the list
    # DEFENSIVELY: an absent/malformed auditor.json or a missing key contributes an empty set.
    blocked_proposals = _blocked_proposals(state_dir, cycle)
    blocked_check = _blocked_check_name(state_dir, cycle)

    # equity (paper: balance + unrealized would need marks; use realized balance as the heat base,
    # matching the deterministic engine's conservative sizing on realized equity).
    equity = account.balance
    peak_equity = max(account.peak_equity, equity)
    open_heat = portfolio_heat(_position_dicts(positions), equity) if equity > 0 else 0.0
    health = PortfolioHealth(equity=equity, peak_equity=peak_equity,
                             open_heat=open_heat, recent_hit_rate=recent_hit_rate)

    held_by_symbol = {p.symbol: p for p in positions}

    # ---- (1) RISK GATE per proposal ----------------------------------------------------------
    # ``approved`` is keyed by SYMBOL so the desk's one-open-per-symbol concentration invariant is
    # enforced INSIDE the batch (not just against symbols already on disk): two proposals for the
    # same coin — whether the same direction (a double-open) or opposite directions (a fully hedged,
    # fee/funding-bleeding book) — can never both reach execution. When a second proposal for a
    # symbol also passes the gate, the HIGHER-CONFIDENCE one wins and the loser is VETOED with an
    # explicit reason (mirrors executor.reconcile's symbol-keyed target semantics).
    rr_state = load_rr_floor(state_dir)
    approved: dict = {}   # symbol -> SizedTrade (at most one per symbol)
    approved_conf: dict[str, float] = {}     # symbol -> winning proposal confidence
    approved_meta: dict = {}                  # symbol -> proposal metadata for journaling
    if not audit_vetoed:
        for raw in raw_proposals:
            meta = dict(raw) if isinstance(raw, dict) else {}
            entry = cards_by_raw.get(meta.get("symbol")) or {}
            funding_rate = float(entry.get("funding_rate", 0.0) or 0.0)
            try:
                ap = AgentProposal.model_validate(raw)
                # The risk gate consumes a TradeProposal, whose stop-side geometry is validated here
                # (AgentProposal does NOT enforce it). Build it inside the guard so a back-to-front
                # stop / degenerate geometry is a VETO, never a crash (fail-loud, Rule 6).
                tp = to_trade_proposal(ap, funding_rate)
            except Exception as e:  # noqa: BLE001 — a malformed proposal is a veto, never a crash
                vetoed.append({"symbol": meta.get("symbol", "?"), "reason": f"malformed: {e}"})
                continue
            # PER-PROPOSAL AUDIT BLOCK: the Auditor flagged THIS proposal's evidence as too thin/
            # inconsistent/degraded (a blocking, not fatal, finding). Skip it — record under `vetoed`
            # with the audit-blocked reason — while the clean proposals continue to the gate.
            if ap.symbol in blocked_proposals:
                vetoed.append({"symbol": ap.symbol, "direction": ap.direction,
                               "reason": f"audit-blocked: {blocked_check}"})
                continue
            spec = _coerce_spec(entry.get("spec"), ap.symbol)
            if spec is None and ("mark" in entry or "atr" in entry):
                spec = _default_spec(ap.symbol)   # context has price data but no explicit spec
            if spec is None:
                vetoed.append({"symbol": ap.symbol, "reason": "no spec in context.json"})
                continue
            # mood -> regime for THIS coin (per-coin override else the market mood)
            mood = _crowd_mood(entry.get("crowd_mood")) if entry.get("crowd_mood") else market_mood
            regime = mood_to_regime(mood.mood, mood.dispersion)
            rr_floor = effective_rr_floor(regime.quadrant, rr_state)
            gi = GateInputs(
                proposal=tp, spec=spec, regime=regime, health=health,
                open_positions=_position_dicts(positions),
                daily_pnl_pct=daily_pct, weekly_pnl_pct=weekly_pct, monthly_pnl_pct=monthly_pct,
                rr_floor=rr_floor,
            )
            decision = evaluate(gi)
            if decision.verdict == "veto" or decision.sized_trade is None:
                vetoed.append({"symbol": ap.symbol, "direction": ap.direction,
                               "reason": decision.reason})
                continue
            if decision.verdict == "resize":
                warnings.append(f"{ap.symbol}: {decision.reason}")
            this_meta = {
                "mood": mood.mood,
                "falsifiable_prediction": ap.falsifiable_prediction,
                "contributing_agents": list(meta.get("contributing_agents") or []),
                "rationale": ap.rationale,
                "confidence": ap.confidence,
                "funding_rate": funding_rate,
                "regime": regime.quadrant,
            }
            # ONE-OPEN-PER-SYMBOL arbitration WITHIN this batch: a symbol already claimed by an
            # earlier gate-passing proposal cannot be opened twice. Keep the higher-confidence side
            # (ties keep the incumbent / first seen) and VETO the loser with an explicit reason so a
            # duplicate or a conflicting long+short never both reach execution.
            prior = approved.get(ap.symbol)
            if prior is not None:
                this_conf = ap.confidence if ap.confidence is not None else 0.0
                prior_conf = approved_conf.get(ap.symbol, 0.0)
                conflict = prior.proposal.direction != ap.direction
                why = ("conflicting-direction" if conflict else "duplicate") + \
                    " symbol proposal — one open per symbol"
                if this_conf > prior_conf:
                    # the incumbent loses; record its veto, then install this proposal.
                    vetoed.append({"symbol": ap.symbol, "direction": prior.proposal.direction,
                                   "reason": why})
                    approved[ap.symbol] = decision.sized_trade
                    approved_conf[ap.symbol] = this_conf
                    approved_meta[ap.symbol] = this_meta
                else:
                    vetoed.append({"symbol": ap.symbol, "direction": ap.direction, "reason": why})
                continue
            approved[ap.symbol] = decision.sized_trade
            approved_conf[ap.symbol] = ap.confidence if ap.confidence is not None else 0.0
            approved_meta[ap.symbol] = this_meta

    # ---- (2) MANAGEMENT REVIEW on held positions (close / reduce). ALWAYS runs (risk-decreasing
    # closes/reduces must execute even under an audit veto). hold = no-op. -----------------------
    surviving: list[Position] = list(positions)
    # symbols whose held position the Decider ALREADY acted on this cycle (a close/reduce that
    # executed). The ungrounded backstop skips these — that path already handled the position.
    managed_symbols: set[str] = set()
    for m in management:
        if not isinstance(m, dict):
            continue
        sym = m.get("symbol")
        action = (m.get("action") or "hold").lower()
        pos = held_by_symbol.get(sym)
        if pos is None or action == "hold":
            continue
        entry = cards_by_raw.get(sym) or {}
        mark = entry.get("mark")
        if mark is None and exchange is not None:
            try:
                mark = exchange.mark_price(_unified(sym))
            except Exception:  # noqa: BLE001 — no mark -> cannot close this cycle (fail-safe keep)
                mark = None
        # MONEY-SAFETY: a NON-POSITIVE mark (0/0.0/negative — a data glitch) is NOT a real price.
        # Treat it as missing: closing/reducing at it would book a fabricated catastrophic loss at
        # price ~0. SKIP the management action, KEEP the position, and warn (fail-loud, Rule 6).
        try:
            mark_val = float(mark) if mark is not None else None
        except (TypeError, ValueError):
            mark_val = None
        if mark_val is None or mark_val <= 0:
            warnings.append(f"{sym}: management {action} skipped — no valid mark available "
                            f"(mark={mark!r})")
            continue
        mark = mark_val
        funding_rate = float(entry.get("funding_rate", 0.0) or 0.0)
        if action == "close":
            ct = close_at_mark(pos, float(mark), funding_rate=funding_rate, funding_events=1,
                               slippage_bps=slippage_bps)
            account.balance += ct.realized_pnl
            closed.append({"symbol": sym, "reason": "management_close",
                           "realized_pnl": ct.realized_pnl})
            surviving = [p for p in surviving if p.symbol != sym]
            managed_symbols.add(sym)
            if pos.decision_id:
                _patch_close(memory_dir, pos.decision_id, ct, now)
        elif action == "reduce":
            frac = float(m.get("reduce_fraction", 0.5) or 0.5)
            frac = min(1.0, max(0.0, frac))
            if frac <= 0:
                continue
            # bank the trimmed slice at mark; shrink the live position's qty/margin proportionally
            slice_pos = pos.model_copy(update={"qty": pos.qty * frac})
            ct = close_at_mark(slice_pos, float(mark), funding_rate=funding_rate, funding_events=1,
                               slippage_bps=slippage_bps)
            account.balance += ct.realized_pnl
            kept_qty = pos.qty * (1.0 - frac)
            reduced = pos.model_copy(update={"qty": kept_qty, "margin": pos.margin * (1.0 - frac)})
            surviving = [reduced if p.symbol == sym else p for p in surviving]
            managed_symbols.add(sym)
            closed.append({"symbol": sym, "reason": "management_reduce",
                           "fraction": frac, "realized_pnl": ct.realized_pnl})

    positions = surviving
    held_symbols = {p.symbol for p in positions}

    # ---- (3) CONSOLIDATION on the approved set: CVaR de-risk + gross-heat cap ------------------
    # drop any approved trade for a symbol we already hold (no stacking; the gate opens fresh
    # entries only — held symbols are managed by the close/reduce review above).
    fresh = [st for st in approved.values() if st.proposal.symbol not in held_symbols]
    n_pre = len(fresh)
    if fresh and not audit_vetoed:
        recent_returns = returns_series(state_dir)
        cvar_mult = cvar_risk_multiplier(recent_returns)
        if cvar_mult != 1.0:
            warnings.append(f"CVaR de-risk: batch scaled x{cvar_mult}")
        # gross-heat cap = the regime cap implied by the market mood's quadrant; use the strictest
        # already-enforced per-trade caps via consolidate's max_heat. Reserve heat already used by
        # held positions so the new batch + book stays within the cap.
        from futures_fund.policy import caps_for
        market_regime = mood_to_regime(market_mood.mood, market_mood.dispersion)
        max_heat = caps_for(market_regime, health).max_heat
        used_heat = portfolio_heat(_position_dicts(positions), equity) if equity > 0 else 0.0
        batch_cap = max(0.0, max_heat - used_heat)
        fresh = consolidate(fresh, equity, batch_cap, cvar_mult=cvar_mult)
        if len(fresh) < n_pre:
            warnings.append(f"consolidation dropped {n_pre - len(fresh)} dust trade(s) under "
                            f"gross-heat cap {batch_cap:.3f}")

    # ---- (4) EXECUTE survivors (paper) --------------------------------------------------------
    if not audit_vetoed:
        for st in fresh:
            sym = st.proposal.symbol
            meta = approved_meta.get(sym, {})
            decision_id = append_decision(memory_dir, {
                "ts": now, "cycle": cycle, "symbol": sym, "direction": st.proposal.direction,
                "entry": st.proposal.entry, "stop": st.proposal.stop,
                "take_profit": list(st.proposal.take_profits),
                "size": st.qty, "leverage": st.leverage,
                "funding_at_entry": meta.get("funding_rate"),
                "regime": meta.get("regime"),
                "confidence": meta.get("confidence"),
                "rationale": meta.get("rationale"),
                "falsifiable_prediction": meta.get("falsifiable_prediction"),
                "contributing_agents": meta.get("contributing_agents") or [],
                "retrieved_memory_ids": _lesson_ids(state_dir, cycle),
                "crowd_mood": meta.get("mood"),
            })
            pos, _fee = open_position(st, cycle, now, slippage_bps, decision_id=decision_id)
            positions.append(pos)
            opened.append({"symbol": sym, "direction": pos.direction, "qty": pos.qty,
                           "entry": pos.entry, "leverage": pos.leverage,
                           "decision_id": decision_id})

    # ---- (4b) UNGROUNDED-THESIS BACKSTOP: a held position whose direction no longer has a fresh
    # GROUNDED SentimentRead is CLOSED, never coasted (the bug: a SHORT held for cycles "ungrounded"
    # while the Decider keeps holding it). This is a RISK-REDUCING net — it ONLY ever CLOSES, never
    # opens/enlarges, so it weakens no risk limit. It runs even on an audit veto (a close is risk-
    # decreasing) and even when the Decider emitted a HOLD (holding an ungrounded position IS the
    # bug); positions the Decider already closed/reduced this cycle are skipped (handled above).
    positions = _close_ungrounded(
        content_dir, state_dir, cycle, now, positions, managed_symbols,
        cards_by_raw, exchange, account, memory_dir, slippage_bps, closed, warnings)

    # ---- (5) RESTING TRIGGERS: arm new + cancel directed (crypto-only) -------------------------
    triggers_armed, triggers_canceled = _manage_triggers(
        state_dir, new_triggers_raw, cancel_triggers, exchange, warnings,
        allow_arm=not audit_vetoed, cards_by_raw=cards_by_raw, cycle=cycle)

    # ---- persist book + account ---------------------------------------------------------------
    save_positions(state_dir, positions)
    account.peak_equity = max(account.peak_equity, account.balance)
    account.updated_ts = now
    save_account(state_dir, account)
    record_equity(state_dir, now, account.balance, cycle)

    realized_pnl = sum(float(c.get("realized_pnl", 0.0) or 0.0) for c in closed)
    exposure = portfolio_heat(_position_dicts(positions), equity) if equity > 0 else 0.0

    report = {
        "cycle": cycle,
        "ran_at": now.isoformat(),
        "candle": floor4(now).isoformat(),
        "audit_ok": audit_ok,
        "opened": opened,
        "closed": closed,
        "vetoed": vetoed,
        "triggers_armed": triggers_armed,
        "triggers_canceled": triggers_canceled,
        "equity": account.balance,
        "realized_pnl": realized_pnl,
        "exposure": exposure,
        "warnings": warnings,
    }
    if audit_vetoed:
        report["reason"] = "audit veto"
    save_output(state_dir, cycle, "report", report)
    return report


def _unified(raw_symbol: str) -> str:
    """Best-effort raw-id -> unified ccxt symbol (BTCUSDT -> BTC/USDT:USDT) for a live mark lookup.
    Only used opportunistically; offline tests never hit this path."""
    if raw_symbol.endswith("USDT"):
        return f"{raw_symbol[:-4]}/USDT:USDT"
    return raw_symbol


def _read_auditor(state_dir, cycle: int) -> dict:
    """Read the persisted auditor verdict as a dict, fail-soft. Returns {} on any
    absence/parse/shape error (the gate's fatal halt is owned by ``audit_gate_ok``; this is only the
    advisory block list, so it must NEVER raise)."""
    try:
        data = load_output(state_dir, cycle, "auditor")
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _blocked_proposals(state_dir, cycle: int) -> set[str]:
    """The set of proposal symbols the Auditor marked PROPOSAL-BLOCKING for this cycle. Read
    DEFENSIVELY: an absent key / non-list / non-string entries contribute nothing (=> empty set), so
    a schema-drifted or legacy auditor.json simply blocks nothing (the fatal gate is separate)."""
    raw = _read_auditor(state_dir, cycle).get("blocked_proposals")
    if not isinstance(raw, list):
        return set()
    return {s for s in raw if isinstance(s, str)}


def _blocked_check_name(state_dir, cycle: int) -> str:
    """A short label naming WHY proposals were blocked (the failed blocking checks), for the
    vetoed-reason string. Fail-soft to a generic label."""
    mismatches = _read_auditor(state_dir, cycle).get("mismatches")
    if isinstance(mismatches, list):
        names = [m for m in mismatches if isinstance(m, str)]
        if names:
            return ",".join(names)
    return "proposal-blocking finding"


def _lesson_ids(state_dir, cycle: int) -> list[str]:
    """Ids of the lessons ACTUALLY RETRIEVED for this cycle (provenance for the decision). Reads the
    retrieve_lessons_cli output at ``state/cycle/<cycle>/lessons.json`` ({"lessons": [...]}) and
    returns those ids — NOT every lesson in the store (which would overstate provenance). FAIL-SOFT:
    an absent/empty/malformed lessons.json contributes an empty list — never blocks an open."""
    try:
        payload = load_output(state_dir, cycle, "lessons")
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 — provenance is advisory; never break the gate
        return []
    lessons = payload.get("lessons") if isinstance(payload, dict) else None
    if not isinstance(lessons, list):
        return []
    return [lz["id"] for lz in lessons
            if isinstance(lz, dict) and isinstance(lz.get("id"), str)]


def _patch_close(memory_dir, decision_id: str, ct, now: datetime) -> None:
    """Patch the Phase-2 outcome of a discretionary close into the journal (fail-soft)."""
    try:
        from futures_fund.journal import patch_outcome
        patch_outcome(memory_dir, decision_id, {
            "exit_ts": now, "realized_pnl": ct.realized_pnl,
            "fees": ct.exit_fee, "funding_paid": ct.funding, "slippage": ct.slippage,
        })
    except Exception:  # noqa: BLE001
        pass


# A derived stop sits ~1.5x ATR from the trigger on the invalidating side (below a long entry,
# above a short). 1.5x mirrors the Decider's documented ~1.5x-3x ATR stop band (the tight end) and
# the stale-trigger geometry deadband's ATR framing — wide enough to survive flush noise, tight
# enough that the derived 2R TP is reachable.
_TRIGGER_STOP_ATR_MULT = 1.5
# The nearest derived TP is placed at exactly the gate's RR floor (2R from entry-to-stop) so a
# back-filled trigger ARMS at — never below — MIN_RR. The real RR check still runs at fire time.
_TRIGGER_TP_RR = 2.0


def _trigger_atr(card: dict, trig: dict) -> float | None:
    """ATR for a derived stop/TP: the trigger's own `atr` if it supplied one, else the context
    card's `atr`. Returns None when neither is a positive finite number (=> the trigger cannot be
    made survivable and is dropped, never armed at a fabricated zero-ATR geometry)."""
    for src in (trig.get("atr"), card.get("atr")):
        try:
            v = float(src)
        except (TypeError, ValueError):
            continue
        if v > 0 and v == v and v != float("inf"):
            return v
    return None


def _backfill_trigger(raw: dict, card: dict, cycle: int) -> dict:
    """Map/derive the fields a Decider trigger omits into the PendingOrder shape (defense in depth —
    the Decider SHOULD now supply stop/take_profits/atr, but the gate back-fills any it doesn't):

      * `level` -> `trigger_level` (the PendingOrder field name; also tolerate `trigger_price`);
      * `atr` from the trigger, else the context card's atr;
      * `stop` ~1.5x ATR on the INVALIDATING side (below a long trigger, above a short);
      * `take_profits` with the nearest TP at 2R from (trigger_level, stop);
      * `expires_cycle` = current cycle + 2.

    Returns the dict to validate. Raises ValueError when the trigger genuinely cannot be made valid
    (unknown direction, missing/negative level, or no ATR to derive a stop) so the caller drops it
    with a clear warning instead of arming a fabricated-geometry order."""
    d = dict(raw)
    # level -> trigger_level (PendingOrder's field name), tolerating the cy84 trigger_price synonym.
    if "trigger_level" not in d:
        if "level" in d:
            d["trigger_level"] = d.pop("level")
        elif "trigger_price" in d:
            d["trigger_level"] = d.pop("trigger_price")
    direction = d.get("direction")
    if direction not in ("long", "short"):
        raise ValueError(f"unknown direction {direction!r}")
    try:
        level = float(d["trigger_level"])
    except (TypeError, ValueError, KeyError):
        raise ValueError("missing/non-numeric trigger level") from None
    if not (level > 0 and level == level and level != float("inf")):
        raise ValueError(f"non-positive/non-finite level {level!r}")
    d["trigger_level"] = level

    atr = _trigger_atr(card, d)
    if atr is not None:
        d["atr"] = atr

    # derive a stop ~1.5x ATR on the invalidating side if the Decider omitted one.
    if d.get("stop") is None:
        if atr is None:
            raise ValueError("no ATR available to derive a stop")
        gap = _TRIGGER_STOP_ATR_MULT * atr
        d["stop"] = level - gap if direction == "long" else level + gap

    # derive take_profits (nearest TP at 2R) if the Decider omitted them.
    if not d.get("take_profits"):
        try:
            stop = float(d["stop"])
        except (TypeError, ValueError):
            raise ValueError("non-numeric stop, cannot derive TP") from None
        risk = abs(level - stop)
        if risk <= 0:
            raise ValueError("zero risk (stop == level), cannot derive TP")
        tp = level + _TRIGGER_TP_RR * risk if direction == "long" else level - _TRIGGER_TP_RR * risk
        d["take_profits"] = [tp]

    if d.get("expires_cycle") is None:
        d["expires_cycle"] = cycle + 2
    if d.get("created_cycle") is None:
        d["created_cycle"] = cycle
    # `reason` is the Decider's word for the rationale field; map it so provenance survives.
    if "rationale" not in d and "reason" in d:
        d["rationale"] = d.pop("reason")
    return d


def _manage_triggers(state_dir, new_raw, cancel, exchange, warnings, *, allow_arm: bool,
                     cards_by_raw: dict | None = None, cycle: int = 0):
    """Arm new resting triggers (crypto-only, audit-gated) and cancel team-directed ones. Returns
    (armed, canceled). Mirrors the deterministic engine's pending-order store contract. A raw
    Decider trigger {symbol, direction, kind, level, risk_mult, reason} has its missing PendingOrder
    fields (trigger_level/stop/take_profits/atr/expires_cycle) MAPPED/DERIVED via
    ``_backfill_trigger`` before validation, so a normal stop_entry/limit_entry now ARMS instead of
    being dropped as 'malformed'. A trigger that genuinely cannot be made valid (unknown
    symbol/direction, no level, no ATR to derive a stop) is still dropped LOUD."""
    cards_by_raw = cards_by_raw or {}
    orders = load_pending_orders(state_dir)
    n_canceled = 0
    if cancel:
        kept: list[PendingOrder] = []
        for o in orders:
            if any(_cancel_matches(c, o) for c in cancel if isinstance(c, dict)):
                n_canceled += 1
            else:
                kept.append(o)
        orders = kept

    new_orders: list[PendingOrder] = []
    if allow_arm:
        for raw in new_raw:
            if not isinstance(raw, dict):
                continue
            sym = raw.get("symbol")
            card = cards_by_raw.get(sym) or {}
            try:
                d = _backfill_trigger(raw, card, cycle)
                new_orders.append(PendingOrder.model_validate(d))
            except Exception as e:  # noqa: BLE001 — a malformed trigger is dropped LOUD, never crashes
                warnings.append(f"trigger dropped (malformed): {sym or '?'} ({e})")
        # crypto-only: never arm a non-crypto resting order
        if new_orders and exchange is not None and hasattr(exchange, "is_crypto_raw"):
            nc, kept_new = non_crypto_triggers(
                new_orders, {o.symbol: bool(exchange.is_crypto_raw(o.symbol)) for o in new_orders})
            if nc:
                new_orders = kept_new
                for o in nc:
                    warnings.append(f"NON-CRYPTO trigger refused at arm: {o.symbol}")

    save_pending_orders(state_dir, upsert_triggers(orders, new_orders))
    return len(new_orders), n_canceled


def _cancel_matches(c: dict, o: PendingOrder) -> bool:
    """A cancel directive matches an order if the symbol matches and (when given) direction/kind."""
    if c.get("symbol") != o.symbol:
        return False
    if c.get("direction") is not None and c.get("direction") != o.direction:
        return False
    if c.get("kind") is not None and c.get("kind") != o.kind:
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Gate + consolidate + execute the cycle's proposals.")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    ap.add_argument("--content-dir", default=_CONTENT_DIR)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--now", default=None, help="ISO timestamp (UTC); defaults to now")
    ap.add_argument("--slippage-bps", type=float, default=_DEFAULT_SLIPPAGE_BPS)
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    now = (datetime.fromisoformat(args.now.replace("Z", "+00:00"))
           if args.now else datetime.now(UTC))
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)

    exchange = None
    if settings.live:
        from futures_fund.exchange import FuturesExchange
        exchange = FuturesExchange.from_settings(settings)

    report = gate_execute(args.state_dir, args.memory_dir, args.cycle, now,
                          settings=settings, exchange=exchange,
                          slippage_bps=args.slippage_bps, content_dir=args.content_dir)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
