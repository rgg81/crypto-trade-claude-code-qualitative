# Auditor (Deterministic Anti-Hallucination Gate — Documentation)

## Mission
You serve Operation ORACLE (the charter is injected above). The Auditor is **not an LLM** — it is deterministic Python (`futures_fund.sentiment_audit.review_cycle`). On a qualitative sentiment desk the gravest failure mode is a confident agent inventing evidence: a citation to a tweet that does not exist, a claim about a coin the item never mentions, a direction smuggled in from a price chart. This file documents the rule the team cannot argue past: **the content store is GROUND TRUTH, and a fabricated citation is a VETO of the entire cycle.**

## The core promise
Every check **RE-DERIVES from ground truth** and NEVER trusts the agent's own assertion. For every cited `item_id` the Auditor re-resolves the item via `content_store.get_item` and re-checks the claim against what the STORED item actually says — its `coins`, its `published_ts`, its `source`. The agent's word is never taken; the store's record always is. `review_cycle` returns an `AuditVerdict` whose `passed` is the **AND of every check**, with `mismatches` naming exactly the checks that failed.

## Fail-closed
An **ABSENT verdict** (the Auditor never ran, or `state/cycle/<N>/auditor.json` is missing/unparseable) is treated as a **FAIL**, exactly like an explicit veto — absence must halt as hard as a veto. `sentiment_audit.audit_gate_ok` is the deterministic HALT flag the execute step checks BEFORE any fill: if it is not an affirmative pass, nothing trades.

## The nine checks (each re-derived from the content store)
1. **`claim_citations_exist`** — every `Claim.item_id` in every read must RESOLVE in the content store. A claim citing an id no item carries is a HALLUCINATION; the check fails listing the unresolvable ids.
2. **`claim_supports_coin`** — each cited item must actually TAG the claim's coin (`coin in item.coins`, re-read from the store). An item that exists but is about a different coin cannot support a claim about THIS coin.
3. **`point_in_time`** — each cited item must have been published STRICTLY BEFORE the read's `as_of_ts` (re-read `published_ts`). An item published at/after the decision anchor is post-decision leakage.
4. **`sentiment_range`** — each read's numeric `s` must round-trip its ordinal `level` (the §7.1 mapping): map level->s and re-bucket s->level; both must agree, so a read claiming level `positive` with `s=-1.0` is caught.
5. **`evidence_sufficiency`** — caps lone-tweet over-conviction. Any read with `confidence >= threshold` and a non-neutral stance must cite at least `min_items` DISTINCT EXISTING items spanning at least `min_sources` DISTINCT sources (distinct items/sources re-derived from the store). A high-conviction directional read resting on one tweet or one source FAILS.
6. **`stance_consistency`** — a plan's rating direction must agree in SIGN with the aggregate expert sentiment. The check re-derives the rating's direction (`rating_to_direction`) and the MEAN `s` of the coin's reads; a long plan into materially-bearish reads (or a short into materially-bullish reads) FAILS. `flat` is vacuously consistent.
7. **`no_directional_price_leak`** — **direction must be 100% sentiment, never price/TA.** The check scans the sentiment surface backing each proposal — the reads' rationales, the plan thesis/prediction, and the proposal rationale/prediction — for support/resistance/MA/RSI/MACD/breakout/fib/chart/price-level language. If any proposal's direction rests on such wording it FAILS. (The price card is risk plumbing only; it may never justify a direction.)
8. **`degraded_source_dominance`** — cap conviction on degraded evidence. The degraded-source set is INJECTED by the caller (the Auditor never imports `source_health`, staying decoupled). A high-conviction read whose cited items ALL come from degraded sources rests entirely on degraded evidence and FAILS.
9. **`evidence_grounding`** — a DIRECTIONAL decision must REST on at least one REAL sentiment read for its coin. For every directional plan (`rating_to_direction` not None) and every proposal (always directional), there must be >= 1 coin-matching `SentimentRead` whose citations RESOLVE in the store. A direction conjured from nothing — no grounded read for the coin — FAILS. (`flat` takes no direction, so it needs no grounding.)

## How the team should treat it
- The verdict is **final and fail-closed**. There is no prompt that talks past a veto, and there is no way to "explain away" a fabricated citation — the store either carries the item or it does not.
- **Never weaken a check to make a cycle pass.** A vetoed cycle that invented evidence is the system WORKING: it caught a hallucination before it risked capital. The cure is to cite real, in-store, in-window items that tag the coin — not to relax the Auditor.
- Agents should treat the nine checks as hard constraints while reasoning: cite real `item_ids`, keep `s` and `level` consistent, never let a price/TA phrase carry a direction, and never push a high-conviction directional read on a single or degraded source.

This is a deterministic gate, so there is no JSON output contract and no `## Output` section — `review_cycle` emits the `AuditVerdict` (persisted as `auditor.json`) itself.
