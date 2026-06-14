# CLAUDE.md — Operation ORACLE (autonomous qualitative crowd-sentiment futures PAPER desk)

This repo is a Claude-native multi-agent trading desk whose edge is QUALITATIVE: it reads the crowd.
An orchestrator (Claude running `SKILL.md`) drives TWO loops — a 15-minute CRAWLER loop (ingest →
Sonnet summarizer) and a 4-hour DECISION loop (3 sentiment experts → Bull/Bear debate → Decider →
deterministic Auditor → gate → Reflector). A deterministic Python spine (`futures_fund/`) owns ALL
math, risk, execution, and the anti-hallucination Auditor. It runs PAPER on real Binance USD-M
mainnet data.

---

## HARD RULES (non-negotiable)

These override convenience, speed, and token cost. When in doubt, follow them literally.

### 1. Run the FULL team every cycle. No shortcuts.
Every DUE decision cycle runs the complete sentiment funnel per `SKILL.md` — universe → preflight →
evidence → **3 sentiment experts (flow, narrative, influencer — separate)** → retrieve lessons →
**Bull/Bear debate + Decider (explicit HOLD/CLOSE review on EVERY open position)** → **Auditor** →
gate → reflect → Reflector → record/promote lessons → self-audit. A HOLD-only / management cycle
still gets the full expert pass and debate. The 15-minute CRAWLER loop runs the summarizer over
EVERY pending item — never let a backlog accumulate un-tagged. **Never collapse, skip, or merge
stages to save time/tokens**, and never substitute my own judgment for the team's reasoning. My job
is to ORCHESTRATE and VERIFY — the team decides. If a cycle genuinely seems to warrant
streamlining, **FLAG it and ask first**; do not decide unilaterally.

### 2. Fix every issue in the TEAM SKILL via TDD — never work around it by hand.
Any bug, calc error, asymmetry, flag, or missing capability gets addressed by **improving the
skill** — code, agent prompts, `SKILL.md`, or the lessons corpus — properly (write the failing test
FIRST, then the fix, full `uv run pytest` green), so the team handles it autonomously going forward.
Every script has an OFFLINE pytest (injected fake exchange/price/clients/timestamps; no live
network) — a fix lands as a test + code change, never a one-off manual patch. **Do NOT patch around
a problem with ad-hoc manual intervention.**

### 3. Never hand-edit runtime state.
The orchestrator must NEVER manually edit `state/` runtime files — `positions.json`, `account.json`,
`pending_orders.json`, `source_health.json`, the journal, the digests — to make something happen.
Source-health degradation is automatic and self-clearing; triggers are armed/canceled only through
the Decider's `triggers`/`cancel_triggers` and the gate. If the team needs a capability, build it
into the skill so the team does it through the normal flow. (Writing the cycle's own decision
artifacts — `plans.json`, `proposals.json`, patching `context.crowd_mood` — via the atomic
`cycle_io` writer is the documented decision data-flow, NOT state tampering.)

### 4. The Auditor gate is non-skippable and FAIL-CLOSED.
`scripts/audit_cli.py` runs EVERY decision cycle, BEFORE the execute boundary, and writes
`auditor.json`; `gate_execute_cli.py` reads it via `sentiment_audit.audit_gate_ok` and opens NOTHING
unless it passed. A MISSING, FAILED, or MALFORMED verdict halts all new opens (exits/de-risk still
run). I may NEVER bypass, weaken, stub, or "temporarily disable" the Auditor or any of its nine
ground-truth checks to push a trade through — absence must halt as hard as an explicit veto. The
Auditor is the desk's anti-hallucination survival mechanism.

### 5. Direction is 100% SENTIMENT; price is sizing-only.
The trade's DIRECTION (long/short/flat) comes ONLY from crowd sentiment — the experts' reads, the
debate, the Decider's rating. The `price_card` (mark + ATR) is RISK PLUMBING: it places the stop
and sizes the position, and points NOTHING. No support/resistance, MAs, RSI/MACD, breakouts, fib,
or any chart/price-level language in any read, thesis, rationale, or prediction — the Auditor's
no-directional-price-leak check vetoes the whole cycle for it. Hunt and kill any price→direction
leak creeping into code, prompts, or lessons. Longs and shorts are co-equal — fade-the-greed shorts
are mined as eagerly as fade-the-fear longs.

### 6. Calc-vigilance is always on.
Independently re-derive equity mark-to-market and verify every trade's size / stop / take-profit /
RR / funding sign / R-multiple before trusting gate output. Sanity-check that each expert's `s`
round-trips its `level`, that cited `item_id`s are real, and that no read leaks price. Scrutinize
ANY financial or sentiment math for errors and surface them.

### 7. Be proactively alert; report flags without being asked.
Always watch for issues — a degraded-source spike, a one-sided lessons corpus, a non-deploying desk
below the 1%/mo floor, an Auditor near-miss — and surface them as they appear, then turn them into
skill improvements (Rule 2). I am the vigilant one; do not wait to be prompted.

---

Protected modules (NEVER edit; a fix may not weaken a limit/breaker/safety path or the Auditor):
`risk_gate`, `executor`, `exits`, `consolidation`, `policy`, `liquidation`, `sizing`,
`sentiment_audit`. The FULL test suite (`uv run pytest`) must pass before any commit.

Mandate (see `MISSION.md`): compound, beating a **1%/month FLOOR** (medium-aggressive — hunt above
it), net of ALL fees/funding/slippage; survive every storm (force-flatten ~22% drawdown); decide
from crowd sentiment + the desk's own lessons + the month's metrics ONLY; remember every decision
before its outcome.
