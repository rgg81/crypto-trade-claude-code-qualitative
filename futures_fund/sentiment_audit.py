"""The deterministic anti-hallucination AUDITOR — the qualitative desk's defining safety mechanism.

This mirrors the discipline of the market-neutral repo's adversarial reviewer
(``futures_fund/reviewer.py`` there): every check RE-DERIVES from GROUND TRUTH and NEVER trusts the
artifact's own claims. The content store IS ground truth — an agent's :class:`SentimentRead`,
:class:`ResearchPlan` and :class:`AgentProposal` only assert; the auditor re-resolves every cited
item via :func:`content_store.get_item` and re-checks the §7 sentiment discipline against the stored
items it claims to rest on.

PER-PROPOSAL SCOPING (PRECISE, not all-or-nothing). The auditor no longer ANDs every finding over
every read into one whole-cycle veto. Instead it computes the set of TRADED coins (the base tickers
of the proposals) and classifies each finding by where it lands:

* FATAL — a TRADED coin's supporting evidence (or the traded proposal itself) rests on fabricated /
  leaked / ungrounded ground truth. Sets ``verdict.passed = False`` (whole-cycle veto). These are
  the genuine hallucinations the desk must never trade on.
* PROPOSAL-BLOCKING — a TRADED proposal's evidence is too thin / inconsistent / degraded to act on,
  but it is not a fabrication. ``verdict.passed`` stays True (if no FATAL) and the specific proposal
  symbol is added to ``verdict.blocked_proposals`` so the execute gate SKIPS just that one.
* ADVISORY — any finding for a NON-traded coin's read. There is no trade to protect, so it neither
  vetoes nor blocks; it is collected in ``verdict.advisories`` (visible/loggable) and the offending
  read is NEUTRALISED (it cannot influence grounding/stance/etc. downstream).

``passed`` therefore means "no FATAL findings". An ABSENT verdict (the auditor never ran / its file
is missing or unparseable) is still treated as a FAIL — absence must halt as hard as an explicit
fatal veto, so a skipped auditor can never let a hallucinated book through (:func:`audit_gate_ok` is
fail-closed).

The auditor is decoupled from source-health: ``degraded_sources`` is INJECTED by the caller (the
CLI), not imported here, so the auditor has no dependency on the health subsystem.

NO check may throw on malformed input — every external lookup is fail-soft (mirrors
``vendors.fetch_news``): an item that cannot be resolved is recorded as a finding (FATAL on a traded
coin, advisory otherwise), never raised into the check loop.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.content_store import ContentItem, get_item
from futures_fund.contracts import (
    AgentProposal,
    Claim,
    ResearchPlan,
    SentimentRead,
    rating_to_direction,
)
from futures_fund.cycle_io import cycle_dir
from futures_fund.sentiment_decay import (
    level_to_s,  # noqa: F401 — §7.1 anchor map, re-exported for callers building/auditing reads
    s_to_level,
    validate_point_in_time,
)

__all__ = [
    "AuditCheck",
    "AuditVerdict",
    "audit_gate_ok",
    "level_to_s",
    "review_cycle",
    "s_to_level",
]

# Re-export so callers can use the desk's single SentimentPlan alias name through the auditor.
SentimentPlan = ResearchPlan


class AuditCheck(BaseModel):
    """One deterministic check's verdict. `passed` is the re-derived comparison, never the
    artifact's own claim; `detail` records what was re-derived (which id/coin failed and its
    classification — fatal | blocking | advisory)."""

    name: str
    passed: bool
    detail: str = ""


class AuditVerdict(BaseModel):
    """The per-proposal-scoped verdict of every :class:`AuditCheck`.

    * ``passed`` — True iff there are NO FATAL findings (a trade resting on fabricated/leaked/
      ungrounded evidence). This is the deterministic HALT flag :func:`audit_gate_ok` reads.
    * ``blocked_proposals`` — symbols of TRADED proposals the gate must SKIP (a proposal-blocking
      finding: thin/inconsistent/degraded evidence). ``passed`` can still be True with these set.
    * ``advisories`` — human-readable findings for NON-traded coins (no trade to protect); these
      neither veto nor block, they are surfaced for logging.
    * ``mismatches`` — exactly the names of the checks that produced a FATAL or BLOCKING finding
      (an advisory-only check is NOT a mismatch). Lets a caller log/route without re-scanning.
    """

    passed: bool
    checks: list[AuditCheck]
    mismatches: list[str] = Field(default_factory=list)
    blocked_proposals: list[str] = Field(default_factory=list)
    advisories: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _norm_coin(coin: str) -> str:
    """Canonical coin token for comparison (upper, stripped). The store tags coins upper-case
    (``content_store._pointer``/index), so the auditor normalises both sides before comparing."""
    return str(coin).strip().upper()


# Quote/settlement suffixes a raw exchange symbol (e.g. BTCUSDT, ETH/USDT:USDT) wraps its base coin
# in. Proposals carry the raw exchange id while reads/plans carry the bare coin token, so the
# auditor strips these to recover the base coin for cross-surface matching.
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BUSD", "PERP")


def _coin_of(symbol: str) -> str:
    """Map a raw exchange symbol to its base coin token (BTCUSDT -> BTC, ETH/USDT:USDT -> ETH).

    Reads/plans use the bare coin while proposals use the exchange id; this recovers the coin so the
    traded-coin set and the price-leak check can link a proposal to the sentiment surface that backs
    it. Strips any ``/quote`` or ``:settle`` decoration, then a trailing quote suffix. Falls back to
    the normalised whole token when nothing strips (an already-bare coin like "ADA")."""
    s = _norm_coin(symbol)
    s = s.split(":", 1)[0].split("/", 1)[0]  # drop :settle then /quote decoration
    for suffix in _QUOTE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


def _resolve(content_dir, item_id: str) -> ContentItem | None:
    """Resolve a cited item id from the content store (ground truth), fail-soft.

    Mirrors ``vendors.fetch_news``: any error resolving the item returns None (the check records it
    as a finding) — a torn/garbage store can never raise into the audit loop."""
    try:
        return get_item(content_dir, item_id)
    except Exception:
        return None


def _all_claims(reads: Iterable[SentimentRead]) -> list[tuple[SentimentRead, Claim]]:
    """Flatten (read, claim) pairs across every read."""
    out: list[tuple[SentimentRead, Claim]] = []
    for r in reads:
        for c in r.claims:
            out.append((r, c))
    return out


def _cited_items(
    content_dir, read: SentimentRead
) -> tuple[list[ContentItem], list[str]]:
    """Resolve every distinct item id this read cites. Returns (resolved_items, unresolved_ids)."""
    seen: set[str] = set()
    items: list[ContentItem] = []
    missing: list[str] = []
    for claim in read.claims:
        for iid in claim.item_ids:
            if iid in seen:
                continue
            seen.add(iid)
            it = _resolve(content_dir, iid)
            if it is None:
                missing.append(iid)
            else:
                items.append(it)
    return items, missing


# Price / technical-analysis language: a sentiment desk decides DIRECTION from sentiment only
# (§ direction = 100% sentiment). Any price/TA justification in a rationale/thesis/prediction that
# backs a proposal's direction is a leak and is vetoed.
#
# 'support'/'resistance' are ALSO everyday narrative words (community support, governance
# resistance), so a bare hit must NOT veto a legitimate sentiment rationale. We only treat
# support/resistance as a price LEAK when it carries a price/level context — a money/number token
# ($63k / 1.20 / 200-day) adjacent to it, or the explicit TA phrasing 'support level',
# 'support zone', 'support/resistance', 'horizontal/key/trend support'. 'chart' is scoped to
# 'chart pattern' / 'price chart' (a bare 'chart' is too common to veto on). The unambiguous
# indicators (RSI, MACD, moving average, fib retracement, breakout level) always leak.
_LEVEL_WORD = r"(?:support|resistance)"
_NUM = r"\$?\d[\d,]*(?:\.\d+)?\s*(?:k|m|bn)?"
_PRICE_LEAK_RE = re.compile(
    rf"""
    \b{_LEVEL_WORD}\s+(?:level|levels|zone|zones|line|trendline)\b   # 'support level', 'resistance zone'
    | \bsupport\s*/\s*resistance\b                                   # 'support/resistance'
    | \b(?:horizontal|key|major|trend|trendline|overhead)\s+{_LEVEL_WORD}\b  # 'horizontal support'
    | {_NUM}\s+{_LEVEL_WORD}\b                                       # '$63k support', '1.20 resistance'
    | \b{_LEVEL_WORD}\s+(?:at|near|around)\s+{_NUM}                  # 'support at $63k'
    | \bmoving\s+average\b
    | \b\d+\s*-?\s*day\s+(?:moving\s+average|MA|SMA|EMA)\b           # '200-day moving average'
    | \bRSI\b
    | \bMACD\b
    | \bbreakout\s+level\b
    | \bfib(?:onacci)?\s+retracement\b
    | \bchart\s+pattern\b
    | \bprice\s+chart\b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _has_price_leak(text: str) -> bool:
    """True if the text justifies direction with price/TA language (the leak the desk forbids)."""
    if not text:
        return False
    return _PRICE_LEAK_RE.search(text) is not None


# --------------------------------------------------------------------------- #
# findings — the per-coin unit the classifier routes                          #
# --------------------------------------------------------------------------- #


class _Finding(BaseModel):
    """One violation a check found, TAGGED with the coin it pertains to so :func:`review_cycle` can
    route it (fatal on a traded coin, advisory on a non-traded one). ``proposal_symbol`` is set when
    a finding is tied to a specific proposal/plan symbol (price-leak / grounding / stance), so the
    classifier can name the exact proposal to block; otherwise it is derived from the coin."""

    coin: str
    detail: str
    proposal_symbol: str | None = None


# Each check returns (clean_detail, findings). ``clean_detail`` is the message used when the check
# produced NO findings (kept identical to the prior wording for readability/back-compat).


def _check_claim_citations_exist(
    content_dir, reads: list[SentimentRead]
) -> tuple[str, list[_Finding]]:
    """Check 1 — every ``Claim.item_id`` in every read must resolve via the content store.

    The store IS ground truth: a claim that cites an id no item carries is a HALLUCINATION. We
    re-resolve every id (never trust that the agent's citation exists). A finding is tagged with the
    read's coin so a hallucination on a TRADED coin is fatal but one on a non-traded coin is only
    advisory."""
    findings: list[_Finding] = []
    for read in reads:
        seen: set[str] = set()
        bad: list[str] = []
        for claim in read.claims:
            for iid in claim.item_ids:
                if iid in seen:
                    continue
                seen.add(iid)
                if _resolve(content_dir, iid) is None:
                    bad.append(iid)
        if bad:
            findings.append(_Finding(
                coin=read.coin,
                detail=f"{read.coin}/{read.agent} hallucinated / unresolvable item_ids: "
                       + ", ".join(sorted(bad)),
            ))
    return "every cited item_id resolves in the content store", findings


def _check_claim_supports_coin(
    content_dir, reads: list[SentimentRead]
) -> tuple[str, list[_Finding]]:
    """Check 2 — each cited item must actually TAG the claim's coin (coin in item.coins).

    An item exists but is about a different coin cannot support a claim about THIS coin. We re-read
    each resolved item's `coins` and require the read's coin to be present. Unresolved ids are
    skipped here (check 1 owns existence)."""
    findings: list[_Finding] = []
    for read in reads:
        for claim in read.claims:
            claim_coins = [_norm_coin(c) for c in claim.coins] or [_norm_coin(read.coin)]
            for iid in claim.item_ids:
                it = _resolve(content_dir, iid)
                if it is None:
                    continue  # existence is check 1's job
                tagged = {_norm_coin(c) for c in it.coins}
                for coin in claim_coins:
                    if coin not in tagged:
                        findings.append(_Finding(
                            coin=read.coin,
                            detail=f"{iid} not tagged {coin} (tags={sorted(tagged)})",
                        ))
    return "every cited item tags the claim's coin", findings


def _check_point_in_time(
    content_dir, reads: list[SentimentRead]
) -> tuple[str, list[_Finding]]:
    """Check 3 — each cited item must have been published STRICTLY before the read's `as_of_ts`.

    Re-reads each resolved item's `published_ts` and runs ``validate_point_in_time`` against the
    read's decision anchor. An item published at/after the anchor is post-decision leakage."""
    findings: list[_Finding] = []
    for read in reads:
        for claim in read.claims:
            for iid in claim.item_ids:
                it = _resolve(content_dir, iid)
                if it is None:
                    continue
                try:
                    ok = validate_point_in_time(it.published_ts, read.as_of_ts)
                except Exception:  # noqa: BLE001 — malformed ts is a finding, never a crash
                    ok = False
                if not ok:
                    findings.append(_Finding(
                        coin=read.coin,
                        detail=f"PIT leakage: {read.coin}/{read.agent} {iid} published "
                               f"{_safe_iso(it.published_ts)} >= as_of "
                               f"{_safe_iso(read.as_of_ts)}",
                    ))
    return "every cited item published before its read's as_of_ts", findings


def _safe_iso(ts) -> str:
    try:
        return ts.isoformat()
    except Exception:  # noqa: BLE001
        return repr(ts)


def _check_sentiment_range(reads: list[SentimentRead]) -> tuple[str, list[_Finding]]:
    """Check 4 — each read's numeric `s` must round-trip its ordinal `level` (§7.1 mapping).

    The auditor never trusts the stored pairing: it maps the level to its anchor and re-buckets `s`;
    both must agree, so a read claiming level "positive" with s = -1.0 is caught. A mismatch leaves
    the read effectively ungrounded for its coin."""
    findings: list[_Finding] = []
    for read in reads:
        try:
            mismatch = s_to_level(read.s) != read.level
            bucket = s_to_level(read.s)
        except Exception:  # noqa: BLE001 — a malformed s/level is a finding, never a crash
            mismatch, bucket = True, "?"
        if mismatch:
            findings.append(_Finding(
                coin=read.coin,
                detail=f"{read.coin}/{read.agent}: s={read.s} buckets to {bucket} "
                       f"not level={read.level}",
            ))
    return "every read's s round-trips its ordinal level", findings


def _check_evidence_sufficiency(
    content_dir,
    reads: list[SentimentRead],
    *,
    conf_threshold: float = 0.6,
    min_items: int = 2,
    min_sources: int = 2,
) -> tuple[str, list[_Finding]]:
    """Check 5 — caps lone-tweet over-conviction.

    Any read with ``confidence >= conf_threshold`` and a non-neutral stance must cite at least
    `min_items` DISTINCT, EXISTING items spanning at least `min_sources` DISTINCT sources. A
    high-conviction directional read resting on one tweet (or one source) FAILS. Distinct existing
    items / sources are re-derived from the store, never the agent's own count."""
    findings: list[_Finding] = []
    for read in reads:
        if read.stance == "neutral" or read.confidence < conf_threshold:
            continue
        items, _missing = _cited_items(content_dir, read)
        n_items = len(items)
        n_sources = len({it.source for it in items})
        if n_items < min_items or n_sources < min_sources:
            findings.append(_Finding(
                coin=read.coin,
                detail=f"{read.coin}/{read.agent} conf={read.confidence:.2f} "
                       f"stance={read.stance}: {n_items} distinct items / {n_sources} sources "
                       f"(need >= {min_items} items, >= {min_sources} sources)",
            ))
    return "every high-conviction directional read spans enough items/sources", findings


def _check_stance_consistency(
    reads: list[SentimentRead],
    plans: list[ResearchPlan],
    proposals: list[AgentProposal],
    *,
    s_tolerance: float = 0.35,
) -> tuple[str, list[_Finding]]:
    """Check 6 — a plan/proposal direction must agree in sign with the aggregate expert sentiment.

    For each plan/proposal we re-derive the trade direction and compare it against the MEAN `s` of
    the (non-neutralised) reads for that coin. A directional decision whose sign contradicts the
    experts FAILS — e.g. a strong_long while the experts are net-bearish. The agreement test: a long
    needs ``mean_s > -s_tolerance`` and a short needs ``mean_s < +s_tolerance``. A 'flat' rating
    (no direction) is vacuously consistent."""
    by_coin: dict[str, list[float]] = {}
    for r in reads:
        by_coin.setdefault(_norm_coin(r.coin), []).append(r.s)
    means = {c: (sum(v) / len(v) if v else 0.0) for c, v in by_coin.items()}

    findings: list[_Finding] = []

    def judge(direction, coin: str, label: str, sym: str | None) -> None:
        mean_s = means.get(coin)
        if mean_s is None:
            return  # no expert read for this coin — nothing to contradict
        if direction == "long" and mean_s <= -s_tolerance:
            findings.append(_Finding(
                coin=coin, proposal_symbol=sym,
                detail=f"{label} (long) but mean expert s={mean_s:.3f} <= -{s_tolerance}",
            ))
        elif direction == "short" and mean_s >= s_tolerance:
            findings.append(_Finding(
                coin=coin, proposal_symbol=sym,
                detail=f"{label} (short) but mean expert s={mean_s:.3f} >= +{s_tolerance}",
            ))

    for plan in plans:
        direction = rating_to_direction(plan.rating)
        if direction is None:
            continue
        judge(direction, _coin_of(plan.symbol), f"{plan.symbol} rating={plan.rating}", None)
    for prop in proposals:
        judge(prop.direction, _coin_of(prop.symbol),
              f"{prop.symbol} proposal={prop.direction}", prop.symbol)
    return "every plan/proposal direction agrees in sign with aggregate expert sentiment", findings


def _check_no_directional_price_leak(
    reads: list[SentimentRead],
    plans: list[ResearchPlan],
    proposals: list[AgentProposal],
) -> tuple[str, list[_Finding]]:
    """Check 7 — direction must be 100% sentiment, never price/TA.

    A proposal's direction may not be justified by price/technical-analysis language. We scan the
    sentiment surface that backs each proposal — the reads' rationales for that coin plus the plan's
    thesis / falsifiable_prediction — for support/resistance/MA/RSI/MACD/breakout/fib/chart/price-
    level wording. A finding is tied to the proposal (so a leak on a TRADED proposal is fatal; a leak
    on a non-traded coin's read is only advisory)."""
    read_text: dict[str, list[str]] = {}
    for r in reads:
        read_text.setdefault(_norm_coin(r.coin), []).append(r.rationale or "")
    plan_text: dict[str, list[str]] = {}
    for p in plans:
        plan_text.setdefault(_coin_of(p.symbol), []).extend(
            [p.thesis or "", p.falsifiable_prediction or ""]
        )

    findings: list[_Finding] = []
    # proposals first (each tied to its own symbol).
    proposed_coins: set[str] = set()
    for prop in proposals:
        coin = _coin_of(prop.symbol)
        proposed_coins.add(coin)
        surfaces = (
            [prop.rationale or "", prop.falsifiable_prediction or ""]
            + read_text.get(coin, [])
            + plan_text.get(coin, [])
        )
        for text in surfaces:
            if _has_price_leak(text):
                findings.append(_Finding(
                    coin=coin, proposal_symbol=prop.symbol,
                    detail=f"{prop.symbol} direction justified by price/TA language: "
                           f"{text.strip()[:80]!r}",
                ))
                break
    # coins WITHOUT a proposal: scan their reads/plans so a leak there surfaces as an advisory.
    for coin in set(read_text) | set(plan_text):
        if coin in proposed_coins:
            continue
        for text in read_text.get(coin, []) + plan_text.get(coin, []):
            if _has_price_leak(text):
                findings.append(_Finding(
                    coin=coin,
                    detail=f"{coin} sentiment surface uses price/TA language: "
                           f"{text.strip()[:80]!r}",
                ))
                break
    return "no proposal's direction is justified by price/TA language", findings


def _check_degraded_source_dominance(
    content_dir,
    reads: list[SentimentRead],
    degraded_sources: set[str] | None,
    *,
    conf_threshold: float = 0.6,
) -> tuple[str, list[_Finding]]:
    """Check 8 — cap conviction on degraded evidence.

    `degraded_sources` is INJECTED by the caller (the CLI passes the source-health set; the auditor
    never imports source_health). If a high-conviction read (``confidence >= conf_threshold``) cites
    items whose sources ALL fall in `degraded_sources`, the read rests entirely on degraded evidence
    and FAILS. Re-derived from the stored items' sources, never the agent's claim. A read citing
    nothing resolvable is not flagged here (check 1 owns missing citations)."""
    degraded = degraded_sources or set()
    findings: list[_Finding] = []
    if degraded:
        for read in reads:
            if read.confidence < conf_threshold:
                continue
            items, _missing = _cited_items(content_dir, read)
            if not items:
                continue  # nothing resolved — existence is check 1's job
            sources = {it.source for it in items}
            if sources and sources <= degraded:
                findings.append(_Finding(
                    coin=read.coin,
                    detail=f"{read.coin}/{read.agent} conf={read.confidence:.2f} rests only on "
                           f"degraded sources {sorted(sources)}",
                ))
    return "no high-conviction read rests solely on degraded sources", findings


def _check_evidence_grounding(
    content_dir,
    reads: list[SentimentRead],
    plans: list[ResearchPlan],
    proposals: list[AgentProposal],
) -> tuple[str, list[_Finding]]:
    """Check 9 — a DIRECTIONAL decision must REST on at least one real sentiment read for its coin.

    The core anti-hallucination promise: a plan/proposal conjured from nothing must NOT pass. For
    every plan whose rating maps to a direction and every proposal (always directional), we require
    at least one coin-matching :class:`SentimentRead` whose citations RESOLVE in the store (and tag
    the coin) — a read citing nothing real cannot ground a decision. A coin with zero qualifying
    reads is flagged (fatal on a traded proposal, advisory on a non-traded plan)."""
    grounded_coins: set[str] = set()
    for read in reads:
        coin = _norm_coin(read.coin)
        items, _missing = _cited_items(content_dir, read)
        # a read grounds its coin only if at least one cited item RESOLVES AND TAGS the coin.
        if any(coin in {_norm_coin(c) for c in it.coins} for it in items):
            grounded_coins.add(coin)

    findings: list[_Finding] = []
    for plan in plans:
        if rating_to_direction(plan.rating) is None:
            continue  # 'flat' — no directional decision to ground
        coin = _coin_of(plan.symbol)
        if coin not in grounded_coins:
            findings.append(_Finding(
                coin=coin, proposal_symbol=None,
                detail=f"plan {plan.symbol} rating={plan.rating} rests on no grounded sentiment "
                       f"read for {coin}",
            ))
    for prop in proposals:
        coin = _coin_of(prop.symbol)
        if coin not in grounded_coins:
            findings.append(_Finding(
                coin=coin, proposal_symbol=prop.symbol,
                detail=f"proposal {prop.symbol} ({prop.direction}) rests on no grounded sentiment "
                       f"read for {coin}",
            ))
    return "every directional plan/proposal rests on >= 1 grounded sentiment read for its coin", \
        findings


# --------------------------------------------------------------------------- #
# review_cycle — per-proposal scoped classification of all nine checks         #
# --------------------------------------------------------------------------- #


# How each check is classified WHEN it fires for a TRADED coin. FATAL means a trade rests on
# fabricated/leaked/ungrounded evidence (whole-cycle veto). BLOCKING means the specific proposal's
# evidence is too thin/inconsistent/degraded to act on (skip just that proposal). ANY check firing
# for a NON-traded coin is ADVISORY regardless of this map.
_FATAL_CHECKS = frozenset({
    "claim_citations_exist",
    "claim_supports_coin",
    "point_in_time",
    "no_directional_price_leak",
    "evidence_grounding",
})
_BLOCKING_CHECKS = frozenset({
    "evidence_sufficiency",
    "stance_consistency",
    "degraded_source_dominance",
    "sentiment_range",
})


def review_cycle(
    content_dir,
    reads: list[SentimentRead],
    plans: list[ResearchPlan],
    proposals: list[AgentProposal],
    now: datetime,  # noqa: ARG001 — injected decision clock (reserved; PIT uses each read's as_of_ts)
    degraded_sources: set[str] | None = None,
    min_items: int = 2,
    min_sources: int = 2,
    conf_threshold: float = 0.6,
    s_tolerance: float = 0.35,
) -> AuditVerdict:
    """Run every anti-hallucination check (re-derived from the content store, ground truth) and
    classify each finding per-proposal into a single deterministic :class:`AuditVerdict`.

    A finding is FATAL (whole-cycle veto) only when it lands on a TRADED coin's supporting evidence
    (or the traded proposal itself) for a fatal-class check; PROPOSAL-BLOCKING when it lands on a
    traded proposal for a blocking-class check (the proposal symbol is added to
    ``blocked_proposals``); and ADVISORY for any finding on a NON-traded coin (collected in
    ``advisories`` and the offending read neutralised so it cannot ground/skew anything downstream).

    ``passed`` = no FATAL findings. ``degraded_sources`` is INJECTED (the auditor never imports
    source_health), and ``now`` is the injected decision clock — there is no global clock, so the
    auditor is deterministic and offline-testable. NO check throws: a torn store / malformed input
    fails closed (fatal on a traded coin, advisory otherwise)."""
    # TRADED coins = base tickers of the proposals (the only coins with a trade to protect). A
    # proposal-symbol -> coin map lets blocking findings name the exact proposal to skip.
    traded_coins: set[str] = {_coin_of(p.symbol) for p in proposals}
    coin_to_symbol: dict[str, str] = {}
    for p in proposals:
        coin_to_symbol.setdefault(_coin_of(p.symbol), p.symbol)

    # ---- ADVISORY PRE-PASS: a NON-traded coin's read is neutralised so it can never influence the
    # cross-surface checks (grounding/stance) or wrongly contribute to a traded proposal. We run the
    # checks on the TRADED reads only for classification of fatal/blocking, but keep the full set to
    # generate advisories for non-traded coins. The simplest sound implementation: split reads into
    # traded vs non-traded, run the checks twice — once on traded (fatal/blocking) and once on
    # non-traded (advisory) — and run cross-surface checks (stance/grounding/leak) with the traded
    # reads only so a neutralised read cannot skew them.
    traded_reads = [r for r in reads if _coin_of(r.coin) in traded_coins]
    nontraded_reads = [r for r in reads if _coin_of(r.coin) not in traded_coins]
    nontraded_plans = [p for p in plans if _coin_of(p.symbol) not in traded_coins]
    traded_plans = [p for p in plans if _coin_of(p.symbol) in traded_coins]

    advisories: list[str] = []
    blocked: set[str] = set()
    fatal_names: set[str] = set()
    blocking_names: set[str] = set()
    # per-check: did it produce any fatal/blocking finding? (drives AuditCheck.passed + mismatches)
    check_failed: dict[str, bool] = {}
    check_detail_override: dict[str, str] = {}

    def route_finding(name: str, f: _Finding) -> None:
        coin = _coin_of(f.coin)
        if coin not in traded_coins:
            advisories.append(f"[{name}] {f.detail}")
            return
        if name in _FATAL_CHECKS:
            fatal_names.add(name)
            check_failed[name] = True
            check_detail_override.setdefault(name, "")
            check_detail_override[name] += ("FATAL: " + f.detail + "; ")
        else:  # blocking
            blocking_names.add(name)
            check_failed[name] = True
            sym = f.proposal_symbol or coin_to_symbol.get(coin)
            if sym:
                blocked.add(sym)
            check_detail_override.setdefault(name, "")
            check_detail_override[name] += (
                f"BLOCKING ({sym}): " + f.detail + "; ")

    # Each check produces (clean_detail, findings). We run on the FULL read set for the
    # read-scoped checks (so non-traded findings surface as advisories) but feed only TRADED reads
    # into the cross-surface checks so a neutralised non-traded read cannot ground/skew a trade.
    check_specs: list[tuple[str, tuple[str, list[_Finding]]]] = [
        ("claim_citations_exist", _check_claim_citations_exist(content_dir, reads)),
        ("claim_supports_coin", _check_claim_supports_coin(content_dir, reads)),
        ("point_in_time", _check_point_in_time(content_dir, reads)),
        ("sentiment_range", _check_sentiment_range(reads)),
        ("evidence_sufficiency", _check_evidence_sufficiency(
            content_dir, reads,
            conf_threshold=conf_threshold, min_items=min_items, min_sources=min_sources)),
        ("stance_consistency", _check_stance_consistency(
            traded_reads, plans, proposals, s_tolerance=s_tolerance)),
        ("no_directional_price_leak", _check_no_directional_price_leak(
            reads, plans, proposals)),
        ("degraded_source_dominance", _check_degraded_source_dominance(
            content_dir, reads, degraded_sources, conf_threshold=conf_threshold)),
        ("evidence_grounding", _check_evidence_grounding(
            content_dir, traded_reads, plans, proposals)),
    ]

    # stance_consistency / grounding for NON-traded plans must still surface as advisories. Run a
    # second, advisory-only pass for those cross-surface checks on the non-traded surface.
    _, nt_stance = _check_stance_consistency(
        nontraded_reads, nontraded_plans, [], s_tolerance=s_tolerance)
    _, nt_ground = _check_evidence_grounding(
        content_dir, nontraded_reads, nontraded_plans, [])

    checks: list[AuditCheck] = []
    for name, (clean_detail, findings) in check_specs:
        for f in findings:
            route_finding(name, f)
        passed = not check_failed.get(name, False)
        detail = clean_detail if passed else check_detail_override.get(name, "").rstrip("; ")
        checks.append(AuditCheck(name=name, passed=passed, detail=detail))

    # fold the non-traded cross-surface advisories in (these are never fatal/blocking).
    for f in nt_stance:
        if _coin_of(f.coin) not in traded_coins:
            advisories.append(f"[stance_consistency] {f.detail}")
    for f in nt_ground:
        if _coin_of(f.coin) not in traded_coins:
            advisories.append(f"[evidence_grounding] {f.detail}")

    _ = (traded_plans,)  # documented split; cross-surface checks already scope by traded coins.

    mismatches = sorted(fatal_names | blocking_names)
    # preserve check declaration order in mismatches for readability.
    order = [c.name for c in checks]
    mismatches = [n for n in order if n in (fatal_names | blocking_names)]
    return AuditVerdict(
        passed=not fatal_names,
        checks=checks,
        mismatches=mismatches,
        blocked_proposals=sorted(blocked),
        advisories=advisories,
    )


def audit_gate_ok(state_dir, cycle: int) -> bool:
    """Read the persisted ``state/cycle/<N>/auditor.json`` and return its `passed` flag — the
    DETERMINISTIC HALT flag the execute step checks before ANY fill. ``passed`` now means "no FATAL
    findings"; a proposal-blocking finding leaves ``passed`` True (the gate consults
    ``blocked_proposals`` to skip only that proposal).

    FAIL-CLOSED: a MISSING file (the auditor never ran) or an UNPARSEABLE / malformed verdict is
    treated as NOT ok, exactly like an explicit fatal veto — absence must halt as hard as a failed
    verdict, so a skipped auditor can never let a book through."""
    path = Path(cycle_dir(state_dir, cycle)) / "auditor.json"
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        # Fail-closed on a malformed-but-valid verdict: valid JSON whose top-level value is
        # NOT an object (a list/str/int/null/bool) has no `passed` flag we can trust. And the
        # gate must HALT on anything other than a genuine boolean True — a tampered / schema-
        # drifted `passed` ("yes", 1, ["x"]) is a FAIL, never a truthy open.
        if not isinstance(data, dict):
            return False
        return data.get("passed") is True
    except Exception:  # noqa: BLE001 — true fail-closed: any read/parse error HALTS the gate.
        return False
