# Risk Manager (Deterministic Gate — Documentation)

## Mission
You serve Operation ORACLE (the charter is injected above). The Risk Manager is **not an LLM** — it is deterministic Python (`futures_fund.risk_gate.evaluate`). This file documents the survival mechanism so the team understands the rule it cannot argue past: **the LLM team PROPOSES (a sentiment direction + a price-shaped stop), the code gate DISPOSES.** `risk_gate.evaluate` returns a `RiskDecision` and is FINAL.

## The division of labor on this desk
- **Direction comes ONLY from sentiment.** The Decider reads long/short/flat from the crowd, never from price. The price card (mark/ATR) only shapes the STOP and the SIZE.
- **The Decider's proposal is a request, not a decision.** It carries `entry`, an ATR-anchored `stop`, `take_profits`, `atr`, `confidence`, `horizon_hours`, and an optional reduction-only `risk_mult`. None of that sets leverage or size.
- **The gate decides whether, and how large.** It consumes the `AgentProposal` (via `contracts.to_trade_proposal`, which injects the funding rate) and runs the A1 risk math.

## What the gate enforces (advisory summary — the code in `futures_fund.risk_gate` / `policy` / `sizing` is the source of truth)
- **Adaptive sizing keyed to the CROWD MOOD regime.** Position size is computed from the regime x portfolio-health caps. The crowd mood maps to a regime via `models.mood_to_regime`, and the unchanged `policy.caps_for` machinery reads it — crowd EXTREMES (euphoric / capitulation) and a highly-split crowd (dispersion >= 0.6 -> `transition`) land in the tightest caps. **Leverage is the OUTPUT of this computation, never an input** (per the charter). No agent sets leverage or size.
- **Liquidation distance.** The liquidation price must sit safely beyond the stop (the A1 multiple of the stop distance), so a normal stop-out can never be a liquidation. Trades that cannot satisfy this are rejected or down-sized.
- **Reward-to-risk floor.** Proposals must clear **RR >= 2** after costs (the Decider already structures the nearest TP at >= 2R; the gate re-verifies after funding/fees). Thinner trades are rejected.
- **Heat cap.** Aggregate open risk ("heat") is capped; a new trade that would breach the cap is trimmed or rejected.
- **`risk_mult` is reduction-only.** The gate CLAMPS any per-trade `risk_mult` to (0,1] — it can only SHRINK a position, never grow one. Pacing (`press`) raises it toward 1.0 to spend UNUSED budget; it never raises a cap.
- **Circuit breakers (`policy.circuit_breaker`).** Drawdown / loss-streak thresholds can HALT the desk entirely — no new risk until cleared. Pacing NEVER presses while `in_drawdown` (anti-martingale is a hard invariant).

## How the team should treat it
- The gate's verdict is **final and cannot be overridden** by any subagent or the orchestrator. There is no prompt that talks past it.
- Any agent "risk" reasoning is **advisory only**: the Decider anchors the stop to ATR and reports the values the gate verifies, but the gate — not the agent — decides whether and how large the trade is.
- **Never weaken a risk limit to make a trade fit or an error disappear.** If something cannot pass safely, it does not trade — that is the system working, not failing. Survival-first is the whole point: you cannot compound from zero. A rejected marginal trade is a win for the mandate.

This is a deterministic gate, so there is no JSON output contract and no `## Output` section — the gate emits the cycle decision (`RiskDecision`) itself.
