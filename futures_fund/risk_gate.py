from __future__ import annotations

from pydantic import BaseModel, Field

from futures_fund.costs import project_funding, round_trip_fee
from futures_fund.liquidation import liquidation_price, mmr_for_notional
from futures_fund.models import (
    CostEstimate,
    PortfolioHealth,
    RegimeState,
    RiskDecision,
    SizedTrade,
    SymbolSpec,
    TradeProposal,
)
from futures_fund.policy import caps_for, circuit_breaker
from futures_fund.portfolio_risk import position_risk
from futures_fund.sizing import choose_leverage, liq_distance_ratio, qty_from_risk

MIN_RR = 2.0
_RR_EPS = 1e-6  # float tolerance so an exactly-2R proposal isn't vetoed by rounding
MIN_LIQ_DISTANCE_MULT = 2.5


class GateInputs(BaseModel):
    proposal: TradeProposal
    spec: SymbolSpec
    regime: RegimeState
    health: PortfolioHealth
    open_positions: list[dict] = Field(default_factory=list)
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    monthly_pnl_pct: float = 0.0
    # The per-quadrant ADAPTIVE reward:risk floor (rr_floor.effective_rr_floor for the proposal's
    # crowd-mood bucket). Defaults to the constant MIN_RR so a caller that doesn't supply it keeps
    # the legacy 2.0 floor. The gate uses max(MIN_RR-protected, rr_floor): the floor can only ever
    # TIGHTEN above MIN_RR (BAND = (1.6, 2.5)); it is clamped so it can never weaken the 2.0 guard.
    rr_floor: float = MIN_RR
    pay_bnb: bool = False


def _reward_risk(p: TradeProposal) -> float:
    if not p.take_profits:
        return 0.0
    nearest_tp = min(p.take_profits, key=lambda tp: abs(tp - p.entry))
    reward = abs(nearest_tp - p.entry)
    risk = p.risk_per_unit
    return reward / risk if risk > 0 else 0.0


def _build_sized(p: TradeProposal, spec: SymbolSpec, qty: float, leverage: float,
                 *, unclamped_funding: bool = False, pay_bnb: bool = False) -> SizedTrade:
    notional = qty * p.entry
    mmr, maint = mmr_for_notional(notional, spec.mmr_brackets)
    margin = notional / leverage if leverage > 0 else notional
    liq = liquidation_price(p.entry, qty, margin, p.direction, mmr, maint)
    # Taker both legs (conservative); `pay_bnb` applies the BNB fee discount (costs.BNB_DISCOUNT)
    # when the caller models settling fees in BNB. This only LOWERS fees, never weakens a limit.
    fees = round_trip_fee(notional, maker_entry=False, maker_exit=False, pay_bnb=pay_bnb)
    # Per-contract funding interval (Binance uses 4h for many perps, 1h under stress);
    # not the magic 8.
    n_events = max(1, int(p.horizon_hours // p.funding_interval_hours))
    funding = project_funding(notional, p.funding_rate, p.direction, n_events=n_events)
    # Carry visibility (Task 5.1): the legacy gate clamped funding to max(0.0, funding), HIDING
    # the credit a short genuinely RECEIVES on positive funding. With `unclamped_funding` the
    # SIGNED value from project_funding is kept, so a short's carry credit lowers cost (raises RR
    # downstream). This only un-hides a real credit — no limit/breaker is weakened (the RR>=floor
    # geometry check on take-profit/stop never sees this funding figure).
    funding_cost = funding if unclamped_funding else max(0.0, funding)
    cost = CostEstimate(entry_fee=fees / 2, exit_fee=fees / 2, funding=funding_cost)
    return SizedTrade(proposal=p, qty=qty, notional=notional, leverage=leverage,
                      margin=margin, liq_price=liq, cost=cost)


def evaluate(inp: GateInputs, *, unclamped_funding: bool = False) -> RiskDecision:
    p, spec = inp.proposal, inp.spec
    caps = caps_for(inp.regime, inp.health)
    breaker = circuit_breaker(inp.daily_pnl_pct, inp.weekly_pnl_pct,
                              inp.monthly_pnl_pct, inp.health.drawdown_from_peak)
    warnings: list[str] = []

    # 1. Hard stops: bias flat / breakers / zero risk budget
    if caps.bias == "flat" or caps.per_trade_risk_pct <= 0:
        return RiskDecision(verdict="veto",
                            reason=f"risk-off: regime/health forces flat (tier={inp.health.tier})")
    if not breaker.allow_new_entries:
        return RiskDecision(verdict="veto", reason=f"circuit breaker: {breaker.reason}")

    # 2. Reward:risk (tolerate float error so an intended exactly-2R trade isn't vetoed).
    # This floor is PURELY GEOMETRIC (take-profit/stop) and never reads funding/costs, so the
    # carry-visibility unclamp cannot resurrect a trade that failed here (Task 5.1 monotonicity).
    # The effective floor is the STRICTER of the constant MIN_RR and the per-mood adaptive floor —
    # the adaptive floor can only ever TIGHTEN it, never weaken the 2.0 survival guard.
    rr = _reward_risk(p)
    rr_floor = max(MIN_RR, inp.rr_floor)
    if rr < rr_floor - _RR_EPS:
        return RiskDecision(verdict="veto", reason=f"RR {rr:.2f} < min {rr_floor:.2f}")

    # 3. Effective per-trade risk budget (caps × breaker multiplier × optional per-trade reduction)
    # Caution tier (caps already halved) AND the -20% step-down can both apply on the same
    # drawdown — the compounding de-risk is intentional (survival-first).
    # risk_mult is an OPTIONAL per-trade REDUCTION (e.g. half-size an unproven-edge/confirmation
    # starter). CLAMPED to (0, 1] so it can ONLY ever SHRINK a position — it can never increase risk
    # above the policy cap or weaken any limit/breaker. None/0 -> 1.0 (no-op); >1 -> 1.0; <0 -> 0.
    rm = min(1.0, max(0.0, getattr(p, "risk_mult", 1.0) or 1.0))
    risk_pct = caps.per_trade_risk_pct * breaker.risk_multiplier * rm

    # 4. Heat headroom: total open risk vs cap. Conservative — total heat >= any single
    #    correlation cluster's heat, so no unsafe trade slips through. Cluster-aware capping
    #    (treating correlated positions as one) is the consolidation step's job.
    equity = inp.health.equity
    used_heat = sum(position_risk(x["qty"], x["entry"], x["stop"], equity, x.get("direction"))
                    for x in inp.open_positions)
    headroom = max(0.0, caps.max_heat - used_heat)
    if headroom <= 0:
        return RiskDecision(
            verdict="veto",
            reason=f"no heat headroom (used {used_heat:.3f} >= cap {caps.max_heat:.3f})",
        )
    effective_risk_pct = min(risk_pct, headroom)
    if effective_risk_pct < risk_pct:
        warnings.append(f"risk trimmed to heat headroom {headroom:.3f}")

    # 5. Size, leverage (output), liq distance
    qty = qty_from_risk(equity, effective_risk_pct, p.entry, p.stop)
    if qty <= 0:
        return RiskDecision(verdict="veto", reason="computed qty is zero")
    notional = qty * p.entry
    mmr, maint = mmr_for_notional(notional, spec.mmr_brackets)
    leverage = choose_leverage(p.entry, p.stop, qty, p.direction, mmr, maint,
                               caps.max_leverage, MIN_LIQ_DISTANCE_MULT)
    if leverage <= 0:
        return RiskDecision(verdict="veto",
                            reason="cannot satisfy liq-distance rule within leverage cap")

    # 6. min-notional check
    if notional < spec.min_notional:
        return RiskDecision(verdict="veto",
                            reason=f"notional {notional:.2f} < min {spec.min_notional}")

    sized = _build_sized(p, spec, qty, leverage, unclamped_funding=unclamped_funding,
                         pay_bnb=inp.pay_bnb)

    # 7. Final liq-distance assertion
    ratio = liq_distance_ratio(p.entry, p.stop, sized.liq_price, p.direction)
    if ratio < MIN_LIQ_DISTANCE_MULT - 1e-6:
        return RiskDecision(verdict="veto",
                            reason=f"liq distance {ratio:.2f}x < {MIN_LIQ_DISTANCE_MULT}x")

    verdict = "resize" if warnings else "approve"
    reason = "approved" if verdict == "approve" else "; ".join(warnings)
    return RiskDecision(verdict=verdict, reason=reason, sized_trade=sized, warnings=warnings)
