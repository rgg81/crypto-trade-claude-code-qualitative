# 🔮 Operation ORACLE

**A self-improving, self-healing multi-agent crypto-futures trading desk whose edge is QUALITATIVE — it reads the crowd. Built as a Claude Code skill.**

> *We compound a real USD account, beating 1% every month — net of every fee, funding payment, and slip — and survive every storm in between. 1%/month is a FLOOR to beat, not a ceiling. Our edge is the crowd's sentiment: direction is 100% sentiment, price is sizing-only. We read the crowd, and we trade the storm.*
> — [`MISSION.md`](MISSION.md)

A team of specialized LLM agents runs the desk on **Binance USD-M perpetual futures**. The LLM team *reasons about crowd psychology*; deterministic, unit-tested Python owns **all** math, risk limits, execution, and a hard anti-hallucination **Auditor**. It runs as TWO loops: a 15-minute CRAWLER loop that ingests + sentiment-tags content, and a 4-hour DECISION loop that runs the sentiment team and trades. It manages a USD account, remembers and reflects on every decision, and repairs its own code along the way.

`paper-by-default · Python 3.11 · 100% offline tests · ruff-clean`

---

## ⚠️ Disclaimer

This is a **research / educational** project. It is **not financial advice**, makes **no guarantee** of profit, and ships with **no warranty**. Trading leveraged crypto futures can lose you **more than your deposit**. The live path is disabled by default. If you ever connect real capital, you do so entirely at your own risk — start on testnet, and never risk money you can't afford to lose.

---

## How it works — two loops

### The CRAWLER loop (every 15 minutes) — the desk's tape
```
crawl_due_check  →  (if DUE)  crawl_cli  →  Content Summarizer (Sonnet)  →  summarize_apply_cli
```
Ingests content from the source adapters (RSS, Reddit, Nitter, StockTwits, Telegram, YouTube, forums), dedupes + stores it, refreshes per-coin rolling-sentiment digests, and dispatches a Sonnet summarizer to tag each new item with a faithful summary + an ordinal sentiment label. Source health is automatic: a flapping upstream is circuit-broken and self-heals. NEVER trades.

### The DECISION loop (every 4 hours) — the sentiment team
```
 D0  due_check ........... gate on the 4h candle (FRESH/RETRY/SKIP)
 D1  universe_cli ........ core ∪ spiking ∪ held coins
 D2  preflight ........... close stop/TP/liq hits · price the book · scorecard + 1%/mo pacing
 D3  evidence_cli ........ one EvidencePacket per coin (digest + items + non-directional price card)
 D4  3 EXPERTS (Sonnet) .. flow · narrative · influencer → sentiment_reads.json
 D5  retrieve_lessons .... mood-filtered, two-sided, top-K
 D6  BULL ⚔ BEAR (Opus) .. per debate coin → strongest long/keep vs short/close/flat
 D7  DECIDER (Opus) ...... 5-tier plans + gate-ready proposals → plans.json + proposals.json
 D8  audit_cli ........... the deterministic ANTI-HALLUCINATION AUDITOR → auditor.json (FAIL-CLOSED)
 D9  gate_execute_cli .... risk gate × consolidation × execution × journal (audit-gated) → report.json
 D10 reflect_cli ......... winners/losers/declined payload
 D11 REFLECTOR (Opus) .... candidate crowd-psychology lessons → reflection_output.json
 D12 record/promote lessons (idempotent append + statistics-gated promotion)
 D13 self_audit_cli ...... standing invariant panel (must print SELF-AUDIT: OK)
```
Direction comes ONLY from sentiment; the price card sizes the trade and places the stop. The Auditor re-derives every read from the content store (ground truth) and HALTS the cycle on any hallucination, price-leak, or thin-evidence over-conviction.

### The team
| Agent | Model | Role |
|---|---|---|
| **Content Summarizer** | Sonnet | Crawler loop: summarize + sentiment-tag each new content item |
| **Flow** | Sonnet | Mention VOLUME & momentum of chatter |
| **Narrative** | Sonnet | The dominant CATALYST/story and its lean |
| **Influencer** | Sonnet | Credibility-weighted CONTRARIAN crowd-extreme (euphoria/capitulation) |
| **Bull** ⚔ **Bear** | Opus | Strongest long/keep vs short/close/flat sentiment case; each rebuts the other |
| **Decider** | Opus | Judges the debate → 5-tier rating + gate-ready proposals + desk crowd-mood |
| **Risk gate / consolidation / executor** *(deterministic)* | — | The survival layer — code, not persuasion |
| **Auditor** *(deterministic)* | — | Re-derives every read from ground truth; fail-closed; the LLM cannot argue past it |
| **Reflector** | Opus | Post-close attribution → CANDIDATE crowd-psychology lessons (two-sided) |

---

## Quickstart (paper)

Requires [`uv`](https://docs.astral.sh/uv/) and Python 3.11+.

```bash
uv sync                       # install deps + create the venv
uv run pytest                 # the full offline test suite (no network, no live LLM)
uv run ruff check .
```

### Run the CRAWLER loop (every 15 minutes)
```bash
# 1. Is this 15-min slot still due?  (DUE -> continue;  SKIP -> stop, liveness ping)
uv run python scripts/crawl_due_check.py

# 2. Run one ingest tick (writes content/_pending_summaries.json):
uv run python scripts/crawl_cli.py

# 3. Dispatch the Content Summarizer (Sonnet) over content/_pending_summaries.json
#    (via the oracle-desk skill, following SKILL.md);
#    save its JSON array of {item_id, summary, item_sentiment} to content/_summaries.json. Then:
uv run python scripts/summarize_apply_cli.py content/_summaries.json

# (nightly, optional) explicit retention purge:
uv run python scripts/purge_cli.py --retain-days 30
```

### Run the DECISION loop (every 4 hours) — take N from due_check
```bash
uv run python scripts/due_check.py                    # -> DUE FRESH <N> | DUE RETRY <N> | SKIP | ERROR
uv run python scripts/universe_cli.py --cycle N
uv run python scripts/preflight.py --cycle N --symbols BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT
uv run python scripts/evidence_cli.py --cycle N
# --- dispatch flow/narrative/influencer (Sonnet) -> state/cycle/N/sentiment_reads.json ---
uv run python scripts/retrieve_lessons_cli.py --cycle N --regime "capitulation,social-heavy" --k 5
# --- dispatch Bull/Bear (Opus) per debate coin; dispatch Decider (Opus) ->
#     write state/cycle/N/plans.json + proposals.json; patch context.crowd_mood ---
uv run python scripts/audit_cli.py --cycle N          # writes auditor.json (FAIL-CLOSED)
uv run python scripts/gate_execute_cli.py --cycle N   # audit-gated execution -> report.json
uv run python scripts/reflect_cli.py --cycle N
# --- dispatch Reflector (Opus) -> state/cycle/N/reflection_output.json ---
uv run python scripts/record_lessons_cli.py --cycle N
uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm   # per Reflector decision
uv run python scripts/self_audit_cli.py               # must print: SELF-AUDIT: OK
```
Full orchestration (which agent reads what, the exact JSON each writes, the §7 sentiment discipline the Auditor enforces) lives in [`SKILL.md`](SKILL.md). Run the FULL team every cycle — see [`CLAUDE.md`](CLAUDE.md) for the hard rules.

---

## Scheduling (cron)

Both loops are **self-healing**: each polls and gates on its own grid (the 15-min slot / the 4h candle), so a missed fire is picked up by the next poll rather than lost.

```cron
# CRAWLER loop — poll every 15 minutes; crawl_due_check gates on the 15-min slot.
*/15 * * * *  cd /home/roberto/crypto-trade-claude-code-qualitative && uv run python scripts/crawl_due_check.py  # then run crawl_cli + summarizer + apply when DUE (via the oracle-desk skill)

# DECISION loop — poll HOURLY (:07 past the hour); due_check gates on the 4h candle and emits FRESH/RETRY/SKIP.
7 * * * *  cd /home/roberto/crypto-trade-claude-code-qualitative && uv run python scripts/due_check.py  # then run the full decision cycle for the printed N when DUE (via the oracle-desk skill)
```
In practice the cron line wakes the **oracle-desk skill** (the `SKILL.md` orchestrator inside Claude Code); it runs the due-gate first and proceeds through the rest of the loop only when DUE. Take the decision-loop cycle number `N` from `due_check` — never pick it yourself.

---

## Memory & learning

- **Two-phase decision journal** (`memory/`) — each decision is written *before* its outcome, then patched with realized PnL on close.
- **Lessons** — CANDIDATE → VALIDATED (a standing rule) only on recurrence **and** statistical support (a one-sided p-value over the closed-trade R-multiples; <5 trades refused); demoted aggressively so stale vetoes don't ossify. Mood-regime-filtered, polarity-balanced retrieval feeds the debate. The Reflector mines fade-the-greed SHORT lessons as eagerly as fade-the-fear LONG lessons so the corpus stays two-sided.
- **Content store** (`content/`) — a 30-day rolling, sentiment-tagged store of every crawled item, with per-coin decayed-sentiment digests. This is the GROUND TRUTH the Auditor re-derives every read against.

## Quality & testing

- **The full test suite is 100% offline.** Every script has an OFFLINE pytest with injected fake exchange / price / clients / timestamps — no live network, no live LLM. Role files are locked to their JSON contracts and `SKILL.md`'s command references by `tests/test_role_files.py`.
- Built TDD; the deterministic spine (risk, execution, the Auditor) is the survival layer and is never weakened to make a test or a trade pass.

```bash
uv run pytest                 # the whole suite, green
```

## Kill switch
```bash
uv run python -c "from futures_fund.state import set_halt; set_halt('state', True, reason='manual kill')"
```
Halts all new trading immediately. Clear with `set_halt('state', False)`.
