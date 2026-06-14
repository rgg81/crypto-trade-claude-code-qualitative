---
name: oracle-desk
description: Operation ORACLE — run the autonomous qualitative crowd-sentiment Binance USD-M futures desk. Two loops, a CRAWLER loop (every 15m) that ingests + summarizes content, and a DECISION loop (every 4h) that runs the sentiment team and trades. Use when asked to run the sentiment team, run a crawl tick, run a decision cycle, or trade on schedule.
---

# Operation ORACLE — Qualitative Sentiment Trading Desk Orchestrator

You are the **orchestrator** of an autonomous crypto-futures desk whose edge is QUALITATIVE: it reads the crowd. Read `MISSION.md` now and hold it as your charter for the whole run. You conduct the team; you do NOT trade by gut, and you NEVER point a trade with price. Deterministic Python does all math, risk limits, execution, and the anti-hallucination Auditor; your subagents do the sentiment reasoning; YOU choreograph and supervise.

**Prerequisite:** `uv sync` has been run. State + memory + content live under `state/`, `memory/`, `content/` (runtime only). The desk runs **PAPER on real mainnet data** (config `live: false`); coins are selected **dynamically** — a static `core_watchlist` UNION attention-spiking coins UNION every held position.

There are **TWO independent loops**, on two cadences:

- **CRAWLER LOOP — every 15 minutes.** Ingests content from the source adapters, dedupes/stores it, and dispatches the Sonnet summarizer to tag each new item with a summary + ordinal sentiment. It is the desk's tape. It NEVER trades.
- **DECISION LOOP — every 4 hours.** Runs the full sentiment team over the accumulated tape and the desk's lessons + metrics, and trades through the deterministic gate.

The directional read flows ONLY through the sentiment funnel. There is NO TA regime, NO analyst/screen/scout funnel — those are dropped. Price (mark + ATR) is RISK PLUMBING: stop placement and sizing only.

---

## The core conventions (apply to BOTH loops)

- Every CLI is OFFLINE-testable, takes `--state-dir state` / `--content-dir content` defaults, writes JSON atomically (temp + `os.replace` via `cycle_io.save_output` / the content-store pattern), and accepts an injected `--now` ISO timestamp where a clock matters.
- Decision-loop artifacts live under `state/cycle/<N>/<name>.json`. Read/written via `futures_fund.cycle_io.{save_output,load_output,cycle_dir}`.
- **Subagent dispatch:** inject `MISSION.md` verbatim AND `state/cycle/<N>/context.json → scorecard` + `→ pacing` at the top of EVERY decision-loop subagent prompt. Give each subagent ONLY its role file's inputs + the relevant cycle JSON; never your full context. When an agent may cross-check figures, hand it the EXPLICIT `state/cycle/<N>/context.json` with `N` stated — never let it infer the cycle (the current cycle is not yet "served"; an agent left to find context grabs the stale prior cycle).
- Each subagent returns ONLY valid JSON matching its `agents/<role>.md` contract. On malformed JSON, re-dispatch once with the validation error; if it fails again, log it, skip that coin/agent, and continue (cap conviction — never trade on missing analysis).

---

# CRAWLER LOOP — every 15 minutes

The crawler cron fires frequently; this is the tape engine. Run these in order each fire.

### C1 — Due-gate (FIRST action every fire)
```
uv run python scripts/crawl_due_check.py
```
Prints exactly one token: `DUE: <reason>` (the 15-min slot still needs a tick → continue to C2) or `SKIP: <reason>` (this slot is already served → this is a liveness ping; **stop here, do nothing else this fire**). Makes ZERO network calls / ZERO writes, always exits 0; fail-safe DUE on any internal error. If `SKIP`, surface the line and stop.

### C2 — Crawl tick (only if DUE)
```
uv run python scripts/crawl_cli.py
```
One deterministic ingest tick: builds the working universe (`core_watchlist` ∪ attention-spiking coins), fans out the **enabled + healthy** source adapters, dedupes + stores new `ContentItem`s, refreshes per-coin digests, purges past the 30-day retention window, persists source health, and stamps `state/crawl/last_crawl.json` (so the next poll in this slot SKIPs). It writes the WORK QUEUE `content/_pending_summaries.json` — the still-unsummarized items (`item_sentiment is None`) for the universe coins, each a compact `{item_id, source, title, body_excerpt, coins}`. The crawler NEVER calls a model. Prints `crawl ok: universe=… new_items=… degraded=… pending=… -> content/_pending_summaries.json`.

**Source-health degradation handling:** a source that fails fetches is circuit-broken after `k_threshold` (3) consecutive failures with exponential backoff (`source_health.record_err`); `crawl_tick` only ever fans out `enabled_adapters` ∩ `healthy_sources`, so a flapping upstream is skipped automatically and self-heals when its cooldown expires — there is NOTHING to do by hand. The crawler prints the `degraded=` count; if it is persistently high (most sources down), surface it (the decision loop's Auditor will independently down-weight reads dominated by degraded sources). NEVER hand-edit `state/source_health.json` — degradation is automatic and self-clearing.

### C3 — Summarize the queue (Sonnet) — only if `pending > 0`
Dispatch the **Content Summarizer** (model: **sonnet**; role: `agents/content_summarizer.md`) with the contents of `content/_pending_summaries.json`. It reads each `{item_id, source, title, body_excerpt, coins}` row and returns a JSON array of verdicts, one per item:
```json
[{"item_id": "<echo input id unchanged>", "summary": "<<=2 faithful sentences>", "item_sentiment": "very_negative|negative|neutral|positive|very_positive"}]
```
**Save the summarizer's array to `content/_summaries.json`.** The summarizer NEVER touches the store; it only emits this verdict array.

### C4 — Apply the summaries (the ONLY writer of summaries)
```
uv run python scripts/summarize_apply_cli.py content/_summaries.json
```
Deterministically folds each `{item_id, summary, item_sentiment}` back into the store: validates the ordinal label (skipping + logging any invalid label rather than poisoning the store), sets `item.summary`/`item.item_sentiment`/`summarized_ts`, rewrites the day file atomically, refreshes the coin index pointers, and updates the affected per-coin digests ONCE each. Idempotent: items already summarized are skipped, so a re-run is safe. Prints `summaries applied=… skipped=… coins_updated=…`.

### C5 — Retention purge (optional; nightly cron, NOT every tick)
```
uv run python scripts/purge_cli.py --retain-days 30
```
The crawl tick already purges each run; this is the explicit nightly wrapper. Day-granular eviction of everything past the retention window.

**That is the whole crawler loop.** No trading, no cycle dir, no gate.

---

# DECISION LOOP — every 4 hours (self-healing hourly poll)

The desk targets one cycle per 4h Binance candle (UTC grid 00/04/08/12/16/20). A session-only cron can miss a boundary, so the loop **polls hourly** and gates on the candle.

### D0 — Due-gate (FIRST action every poll)
```
uv run python scripts/due_check.py
```
Prints one line-1 token (take the cycle number `N` from here, NEVER pick it yourself):
- **`DUE FRESH <N>`** → run a brand-new cycle end-to-end with cycle `N` (create `state/cycle/<N>/`).
- **`DUE RETRY <N>`** → a prior dir crashed before its gate; re-run and OVERWRITE `state/cycle/<N>/` (safe — the gate reconciles against on-disk positions, cannot double-open). Use this `N`; do NOT increment.
- **`SKIP: <reason>`** (exit 0) → this candle is already served; **do nothing this poll** (liveness ping — surface it and stop).
- **`ERROR: <reason>`** (exit code 2) → do NOT trade; surface/notify and stop.

The gate (D11) stamps each cycle's served candle (`report.json` `candle`/`ran_at`), so **every real run MUST reach D11** — including stand-downs and audit-vetoes — or the next poll double-fires.

### D1 — Build the universe
```
uv run python scripts/universe_cli.py --cycle N
```
Writes `state/cycle/N/universe.json` = `{core, spiking, held, all}` — `core_watchlist` ∪ attention-spiking coins ∪ **every currently-held coin** (a held coin must NEVER drop out of the universe, even if it stopped spiking — it has to flow through to be managed/exited). All lists are sorted, de-duped, upper-case base tickers. ZERO network calls.

### D2 — Preflight (audit exits, price the book, scorecard + pacing)
```
uv run python scripts/preflight.py --cycle N --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT
```
`--symbols` is the comma-separated UNIFIED ccxt symbols for the universe coins (map each base ticker `BTC` → `BTC/USDT:USDT`; ALWAYS include the major anchors BTC + ETH/SOL even as backdrop). It (a) closes any position whose latest COMPLETED 4h bar hit stop/TP/liquidation (paper, patching the journal); (b) prices every symbol — spec + mark + ATR + funding (RISK PLUMBING ONLY); (c) builds per-held-symbol HOLD/CLOSE holding cards (entry, mark, unrealized %, R-progress, bars held, dist-to-stop/liq, the ORIGINAL thesis + falsifiable prediction); (d) assembles the **scorecard** (equity, drawdown, health tier, daily/weekly/monthly PnL %, Sharpe/hit-rate/profit-factor, per-expert hit-rates) and the month-to-date **pacing** directive keyed to the **1%/mo FLOOR** (anti-martingale — never presses in drawdown). Writes `state/cycle/N/context.json`. FAIL-SOFT per symbol.

### D3 — Assemble evidence (one packet per coin)
```
uv run python scripts/evidence_cli.py --cycle N
```
Reads `universe.json → all` and assembles one self-contained `EvidencePacket` per coin → `state/cycle/N/evidence.json` = `{COIN: packet}`. Each packet carries the coin's rolling decayed-sentiment `digest`, its in-window `recent_items` (the ONLY `item_id`s an expert may cite), a `source_breakdown`, and a NON-DIRECTIONAL `price_card` (mark + ATR for stop/size only). FAIL-SOFT: a price fetch that raises for one coin degrades only that card.

### D4 — Three sentiment experts (Sonnet) → `sentiment_reads.json`
Dispatch THREE separate Sonnet subagents over the evidence packets — one per lane (boundaries are by KIND, not source):
- **Flow** (`agents/flow_sentiment.md`) — mention VOLUME & momentum of chatter; `agent="flow"`.
- **Narrative** (`agents/narrative_sentiment.md`) — the dominant CATALYST/story and its lean; `agent="narrative"`.
- **Influencer** (`agents/influencer_sentiment.md`) — credibility-weighted CONTRARIAN crowd-extreme (euphoria/capitulation); `agent="influencer"`.

Inject each with the relevant `EvidencePacket`(s) + `MISSION.md` + the scorecard. Each returns a JSON LIST of `SentimentRead`, one per coin:
```json
{"agent": "flow|narrative|influencer", "coin": "BTC", "stance": "bullish|bearish|neutral",
 "level": "very_positive|positive|neutral|negative|very_negative", "s": 0.0, "confidence": 0.0,
 "claims": [{"text": "...", "item_ids": ["<real id from the packet>"], "coins": ["BTC"]}],
 "rationale": "<no price/TA language>", "as_of_ts": "<copy the packet's as_of_ts verbatim>"}
```
**Concatenate all three experts' lists into one flat array and save it to `state/cycle/N/sentiment_reads.json`** (a `list[SentimentRead]` — exactly what the Auditor loads). Every cited `item_id` MUST exist in the content store and tag that coin; `s` MUST round-trip `level` (§7.1); `stance` sign MUST agree with `s`; direction is 100% sentiment — NO price/TA language. The Auditor (D9) re-derives all of this from ground truth and HARD-VETOES the cycle on any violation.

### D5 — Retrieve lessons
```
uv run python scripts/retrieve_lessons_cli.py --cycle N --regime "<crowd-mood>,<source-mix>" --tags <setup tags> --k 5
```
Pass BOTH contexts to `--regime` (comma-separated): the crowd MOOD label (`euphoric`/`greedy`/`neutral`/`fearful`/`capitulation`) AND the SOURCE-MIX label (e.g. `social-heavy`) — the corpus mixes both vocabularies. Writes `state/cycle/N/lessons.json` = `{"lessons": [...]}` (top 3–7 VALIDATED/relevant, polarity-balanced). Inject the returned set into the Bull, Bear, and Decider prompts — the team must reason WITH its past lessons.

### D6 — Bull vs Bear debate (Opus, per debate coin)
The **debate set = every coin with a non-trivial sentiment read ∪ EVERY held coin** (a holding is never dropped — it gets an explicit HOLD/CLOSE framing). For each coin in the debate set:
1. Dispatch **Bull** (model: **opus**; `agents/bull.md`) with that coin's `EvidencePacket` + its three `SentimentRead`s + retrieved lessons → strongest LONG/keep case.
2. Dispatch **Bear** (model: **opus**; `agents/bear.md`) with the same + the Bull's thesis → strongest SHORT/close/flat case, rebutting the Bull. **Flat must be EARNED, never defaulted.**
3. (High-dispersion / low-confidence: run one more Bull→Bear rebuttal round.)

Each returns ONLY: `{"coin": "BTC", "thesis": "...", "key_points": ["..."], "confidence": 0.0}`. **For a HELD coin, frame the debate as HOLD vs CLOSE** — inject its holding card from `context.json → holdings`: is the position's ORIGINAL falsifiable prediction still intact (→ HOLD) or broken (→ CLOSE)? Keep the per-coin Bull/Bear outputs in-memory to hand to the Decider. Direction is 100% sentiment; longs and shorts are co-equal — never rate a short lower just because it is a short.

### D7 — Decider (Opus) → `plans.json` + `proposals.json` (+ patch `context.crowd_mood`)
Dispatch the **Decider** (model: **opus**; `agents/decider.md`) with every coin's Bull+Bear theses, its `SentimentRead`s + `EvidencePacket` (price card for stop/size geometry ONLY), the retrieved lessons, and the injected `pacing` directive + health. It returns ONE JSON object:
```json
{"plans": [{"symbol": "BTCUSDT", "rating": "strong_long|long|flat|short|strong_short", "confidence": 0.0,
            "thesis": "...", "falsifiable_prediction": "..."}],
 "proposals": [{"symbol": "BTCUSDT", "direction": "long|short", "entry": 0.0, "stop": 0.0,
                "take_profits": [0.0], "atr": 0.0, "confidence": 0.0, "horizon_hours": 4.0,
                "rationale": "<NO price/TA>", "falsifiable_prediction": "...",
                "confirmation": true, "risk_mult": 1.0}],
 "management": [{"symbol": "BTCUSDT", "action": "close|reduce|trail", "reduce_fraction": 0.5,
                 "new_stop": 0.0, "reason": "..."}],
 "triggers": [{"symbol": "BTCUSDT", "direction": "long|short", "kind": "stop_entry|limit_entry",
               "level": 0.0, "risk_mult": 1.0, "reason": "..."}],
 "crowd_mood": {"mood": "euphoric|greedy|neutral|fearful|capitulation", "dispersion": 0.0, "rationale": "..."}}
```
DIRECTION COMES ONLY FROM SENTIMENT (the rating → direction via `rating_to_direction`); the price card shapes only the stop (~1.5–3× ATR) and the size; nearest TP ≥ 2R. A `flat` plan emits NO proposal. Honor `pacing.mode` (`press` lowers the take-it bar above the 1% floor; never press in drawdown). Split and persist the Decider's object into the THREE canonical artifacts the gate + Auditor read, using the atomic cycle-io writer (the cycle's decision data-flow, NOT runtime-state editing):

- `state/cycle/N/plans.json` ← the bare `plans` LIST (`list[ResearchPlan]` — what the Auditor loads).
- `state/cycle/N/proposals.json` ← `{"proposals": [...], "management": [...], "triggers": [...]}` (the gate reads `.proposals`/`.management`/`.triggers`; the Auditor reads `.proposals`). On a stand-down/flat-only cycle this is `{"proposals": [], "management": [...], "triggers": []}` — the **`management` list is mandatory** (an omitted/null `management` is coerced to empty = holdings KEPT, never close-everything).
- Patch the Decider's `crowd_mood` into `state/cycle/N/context.json` (the gate reads `context.crowd_mood` to map mood → regime caps via `models.mood_to_regime`). Do this atomically, e.g.:
  ```
  uv run python -c "from futures_fund.cycle_io import load_output, save_output; import json,sys; c=load_output('state',$N,'context'); c['crowd_mood']=json.load(open('state/cycle/$N/_decider_mood.json')); save_output('state',$N,'context',c)"
  ```
  (write the Decider's `crowd_mood` object to `state/cycle/N/_decider_mood.json` first). This is part of the documented cycle data-flow; it is NOT hand-editing `positions.json`/`account.json`/`pending_orders.json` (those are forbidden — Rule 3).

### D8 — Run the Auditor (MANDATORY, fail-closed) → `auditor.json`
```
uv run python scripts/audit_cli.py --cycle N
```
The deterministic ANTI-HALLUCINATION Auditor — the desk's hard gate. It loads `sentiment_reads.json` (`list[SentimentRead]`), `plans.json` (or per-coin `plan_<COIN>.json`), and `proposals.json` (`.proposals`), injects the currently-degraded sources, and re-derives **nine ground-truth checks** from the content store (every cited `item_id` exists + tags its coin; point-in-time; `s`↔`level` range; evidence sufficiency / ≥2 items across ≥2 sources for high conviction; stance consistency; **no directional price leak**; evidence grounding; degraded-source dominance). Writes `state/cycle/N/auditor.json` (the `AuditVerdict` — exactly where the gate reads it). **FAIL-CLOSED:** a MISSING or MALFORMED reads/plans/proposals file does NOT crash and does NOT silently pass — it writes a FAILED verdict so the execute gate halts as on an explicit veto. Exit code is non-zero on a failed verdict, but the persisted `auditor.json` is the authoritative gate flag either way.

### D9 — Gate + consolidate + execute + journal (DETERMINISTIC) → `report.json`
```
uv run python scripts/gate_execute_cli.py --cycle N
```
The single execution authority. Pipeline (fail-closed, survival-first):
1. **AUDIT GATE FIRST.** Reads `auditor.json` via `sentiment_audit.audit_gate_ok`. If the Auditor vetoed / never ran / its verdict is malformed → **NOTHING is opened** (exits/closes + de-risk still run; `report.reason = "audit veto"`). This gate is **non-skippable and fail-closed** — you CANNOT override it.
2. Loads `context.json` (per-symbol spec/mark/atr/funding, `crowd_mood`, scorecard, pnl), `positions.json`, `account.json`.
3. Maps `crowd_mood` → `RegimeState` via `mood_to_regime` (per-coin override else market mood).
4. Per proposal: builds a `TradeProposal`, assembles `GateInputs`, calls `risk_gate.evaluate` (RR floor, regime × health caps, liq-distance, heat). Vetoes/resizes recorded; a malformed proposal is dropped (the rest still execute).
5. Applies the team's `management` (close/reduce/trail) on held positions; arms/cancels resting `triggers` via `pending_orders` (crypto-only).
6. CONSOLIDATES the approved set: gross-heat cap (reserving held-position heat) + CVaR de-risk.
7. EXECUTES survivors (paper); journals a Phase-1 decision per open carrying `contributing_agents`, `retrieved_memory_ids`, `falsifiable_prediction`, `crowd_mood`.
8. Writes `state/cycle/N/report.json` with `candle`/`ran_at` run-markers, `opened`/`closed`/`vetoed`/`triggers_*`/`equity`/`exposure`/`warnings`.

Deterministic safety enforced regardless of agent output: a −22%-ish drawdown force-flattens the book; the audit veto blocks all opens; a held coin the team flips is never stacked long+short. OFFLINE by construction — every price figure comes from `context.json`.

### D10 — Reflect (emit the learning payload)
```
uv run python scripts/reflect_cli.py --cycle N
```
Writes `state/cycle/N/reflection_input.json` (closed winners vs losers, PLUS `declined_edge_setups` and `missed_opportunities`). If there are closed trades OR missed opportunities, continue to D11.

### D11 — Reflector (Opus) → `reflection_output.json`
Dispatch the **Reflector** (model: **opus**; `agents/reflector.md`) with `reflection_input.json` + the journaled reads/debate behind each closed decision + `MISSION.md`. It returns CANDIDATE crowd-psychology lessons:
```json
{"lessons": [{"text": "...", "polarity": "restrictive|enabling|process",
              "regime": "<crowd mood quadrant or null>", "tags": ["..."], "importance": 5,
              "provenance": ["<decision_id>"]}]}
```
**Save to `state/cycle/N/reflection_output.json`.** When winners or missed opportunities exist, the Reflector MUST mint ≥1 `enabling` lesson (the corpus stays two-sided; it mines fade-the-greed SHORTS as eagerly as fade-the-fear LONGS — a one-way "don't" ratchet is what strands a desk in all-cash).

### D12 — Record + promote lessons (deterministic)
```
uv run python scripts/record_lessons_cli.py --cycle N
```
Idempotently appends the Reflector's `reflection_output.json` lessons to the corpus (by exact text — a DUE RETRY appends each once). Do NOT rely on the LLM to append.

For each EXISTING lesson the Reflector confirmed/demoted/retired against the closed trades:
```
uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire
```
`confirm` is STATISTICS-GATED: a candidate promotes to VALIDATED only once it recurs enough AND the desk's edge is statistically supported (a one-sided p-value re-derived from the closed-trade R-multiples; <5 trades → refused). Demote stale/regime-mismatched VALIDATED lessons aggressively so vetoes don't ossify.

### D13 — Self-audit (standing invariant panel)
```
uv run python scripts/self_audit_cli.py
```
Runs the critical cross-module invariant panel (anti-martingale pacing, gate RR floor, content-store integrity, auditor-gate presence on the latest completed cycle, no-price-leak path sanity). Must print `SELF-AUDIT: OK`. Run it whenever code changed and as a periodic check; the full `uv run pytest` remains the regression backstop.

Finally, present `report.json` to the user: actions taken, current book, equity, risk posture, AND the `pacing` read (is the desk clearing the 1%/mo floor, deploying, improving?).

---

## §7 — Sentiment discipline (the contract the Auditor enforces)

The whole desk rests on grounded, point-in-time, price-blind reads. The Auditor re-derives all of this from the content store; an agent that violates it HALTS the cycle.

### §7.1 — `level` ↔ `s` mapping (must round-trip)
`very_positive` → `s=1.0`; `positive` → `0.5`; `neutral` → `0.0`; `negative` → `-0.5`; `very_negative` → `-1.0`. An expert's numeric `s` must round-trip its ordinal `level`, and `stance` must agree in sign (`bullish` for `s>0`, `bearish` for `s<0`, `neutral` for `s≈0`). A euphoric crowd → the Influencer's CONTRARIAN read is `bearish` / negative `s`.

### §7 — the nine ground-truth checks (re-derived in `sentiment_audit.review_cycle`)
1. Every cited `item_id` exists in the store. 2. Each cited item tags the coin it is cited for. 3. Point-in-time (no item published after the cycle anchor). 4. `s` is in range and round-trips `level`. 5. Evidence sufficiency: a non-neutral read with `confidence ≥ 0.6` cites ≥2 distinct items across ≥2 distinct sources. 6. Stance consistency (sign of `s` ↔ `stance`). 7. **No directional price leak** — no proposal's direction may rest on price/level/chart language; direction is 100% sentiment. 8. Evidence grounding (proposals trace to the sentiment surface). 9. Degraded-source dominance (a read may not rest on degraded sources alone). The verdict `passed` is the AND of all nine; absence is fail-closed.

---

## Scheduling — two self-healing loops
- **Crawler:** poll every 15m (`crawl_due_check` gates on the 15-min slot; an extra tick is low-harm — it dedupes).
- **Decision:** poll hourly (`due_check` gates on the 4h candle; FRESH/RETRY/SKIP). Every real decision run MUST reach D9 (the gate stamps the served candle), or the next poll double-fires. See README "Scheduling" for the cron lines.

## Self-healing (fix in the SKILL, never by hand)
If any `scripts/*` call errors: log it (`futures_fund.repair.log_error` → `state/error-log.jsonl`), diagnose the ROOT cause, fix the CODE via TDD (full `uv run pytest` green before commit), record the repair (`futures_fund.repair.record_repair`), then resume from the failed step or degrade safely (cap conviction / skip the affected coin). A fix to a protected module (`futures_fund.repair.is_protected`) may NEVER weaken a risk limit, disable a breaker, or bypass the Auditor. If it cannot be fixed safely, set the HALT flag (`futures_fund.state.set_halt`) and surface for human review — a paused desk beats a bad trade.
