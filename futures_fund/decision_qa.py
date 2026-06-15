"""The per-cycle DECISION-QA engine — the SELF-IMPROVEMENT subsystem's deterministic monitor.

Where :mod:`futures_fund.sentiment_audit` is the HARD GATE that vetoes a cycle before any fill, this
module is the SOFT, *observational* layer that watches every agent's output cycle-after-cycle and
keeps a rolling reliability score per lane (flow / narrative / influencer). It NEVER blocks a trade
and NEVER weakens an Auditor check — it only RE-DERIVES quality metrics from GROUND TRUTH (the
content store) and feeds a learning loop so a lane that keeps hallucinating loses trust over time.

Everything here is DETERMINISTIC and OFFLINE: the only external read is
:func:`content_store.get_item`, time is taken from the artifacts themselves, and every metric is
FAIL-SOFT — a missing/torn/malformed artifact yields 0/None for its metric and NEVER raises. The
content store is ground truth: a cited ``item_id`` that does not resolve, or resolves to an item NOT
tagged with the claimed coin, is a HALLUCINATED citation (mirrors the auditor's checks 1+2).

The metrics captured per cycle (see :class:`DecisionQA`):

* per-lane stats — total_claims, hallucinated_citations, n_reads, n_nonneutral.
* mislabels — reads whose ``s`` does not round-trip its ``level`` (the §7.1 map; same as the
  auditor's sentiment_range check, re-derived here for observability).
* ungrounded_plans — directional plans (rating != flat) with no coin-matching read whose stance
  matches the plan direction.
* lane_redundancy — per-coin Jaccard overlap of the cited item_id SETS across the lanes (high
  overlap means the lanes are echoing the SAME items, not independently corroborating).
* auditor signals — copies of the persisted verdict's passed / advisory count / blocked count and a
  tally of the check-names that produced a mismatch or advisory.

:func:`update_reliability` then folds the cycle's QA into ``memory/agent_reliability.json`` via an
EWMA so the trust score reacts to recent behaviour but is not whipsawed by a single cycle.
"""
from __future__ import annotations

import json
import os
import re
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.content_store import get_item
from futures_fund.contracts import rating_to_direction
from futures_fund.cycle_io import cycle_dir
from futures_fund.sentiment_decay import s_to_level

__all__ = [
    "DecisionQA",
    "LaneStats",
    "analyze_cycle",
    "update_reliability",
]

# The three sentiment lanes the desk runs. Reliability is tracked per lane.
LANES = ("flow", "narrative", "influencer")

# Quote/settlement suffixes a raw exchange symbol wraps its base coin in (BTCUSDT -> BTC). Mirrors
# sentiment_audit._coin_of so a plan symbol links to the bare-coin reads that back it.
_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BUSD", "PERP")
_DECORATION_RE = re.compile(r"[:/].*$")


def _norm_coin(coin: object) -> str:
    """Canonical coin token (upper, stripped) — matches the store's upper-case index key."""
    return str(coin).strip().upper()


def _coin_of(symbol: object) -> str:
    """Map a raw exchange symbol to its base coin (BTCUSDT -> BTC, ETH/USDT:USDT -> ETH).

    Strips any ``:settle`` / ``/quote`` decoration then a trailing quote suffix, so a proposal/plan
    symbol can be linked to the bare-coin sentiment reads. Falls back to the whole normalised token
    when nothing strips (an already-bare coin like "ADA")."""
    s = _DECORATION_RE.sub("", _norm_coin(symbol))
    for suffix in _QUOTE_SUFFIXES:
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)]
    return s


# --------------------------------------------------------------------------- #
# models                                                                       #
# --------------------------------------------------------------------------- #


class LaneStats(BaseModel):
    """Per-lane quality counters re-derived from ground truth for one cycle."""

    lane: str
    n_reads: int = 0
    n_nonneutral: int = 0
    total_claims: int = 0
    hallucinated_citations: int = 0
    # total_citations is the denominator for the hallucination rate: the number of (claim, item_id)
    # citation occurrences the lane made (NOT the claim count — one claim can cite many items).
    total_citations: int = 0
    # ungrounded directional CONVICTION: the number of NONNEUTRAL reads that rested on ZERO resolving
    # coin-matching citations (an empty/absent claim list, or every cited id hallucinated). Asserting
    # a strong stance on no evidence is unsupported conviction — it counts AGAINST grounding so a
    # zero-citation directional lane can never score a perfect (rate-0) cycle (finding 8).
    ungrounded_nonneutral: int = 0
    # calibration penalty in [0, 1]: the mean STAKED confidence on the lane's high-confidence
    # (>= 0.6) reads that turned out hallucinated/ungrounded (0.0 if the lane made no high-conf read
    # or none were bad). A lane that bets BIG and is wrong is penalised more than one that hedges.
    calibration_penalty: float = 0.0

    @property
    def hallucination_rate(self) -> float:
        """Fraction of evidence UNITS that failed to ground, in [0, 1].

        The denominator is every citation occurrence PLUS every ungrounded directional read (a
        nonneutral read that cited nothing resolving); the numerator is the hallucinated citations
        PLUS those same ungrounded directional reads. So:

        * a lane that cited nothing AND took no directional stance -> 0/0 -> 0.0 (no demonstrated
          failure — a hedged/absent lane is neither rewarded nor punished);
        * a NONNEUTRAL read citing nothing -> at least 1/1 -> > 0.0 (unsupported conviction is
          counted against grounding, never a free perfect score)."""
        units = self.total_citations + self.ungrounded_nonneutral
        bad = self.hallucinated_citations + self.ungrounded_nonneutral
        return (bad / units) if units else 0.0


class DecisionQA(BaseModel):
    """The full per-cycle DECISION-QA snapshot — one JSONL line in ``memory/decision-qa.jsonl``."""

    cycle: int
    lanes: dict[str, LaneStats] = Field(default_factory=dict)
    mislabels: list[str] = Field(default_factory=list)
    ungrounded_plans: list[str] = Field(default_factory=list)
    # mean Jaccard overlap of cited item_id sets across lanes, averaged over coins (0.0 = no overlap
    # / independent lanes; 1.0 = the lanes cite identical item sets for every coin).
    lane_redundancy_mean: float = 0.0
    lane_redundancy_by_coin: dict[str, float] = Field(default_factory=dict)
    # auditor signals (copied from the persisted verdict; all None/0 when the file is absent).
    auditor_passed: bool | None = None
    n_advisories: int = 0
    n_blocked_proposals: int = 0
    mismatch_checks: dict[str, int] = Field(default_factory=dict)
    advisory_checks: dict[str, int] = Field(default_factory=dict)

    @property
    def total_hallucinated(self) -> int:
        return sum(ls.hallucinated_citations for ls in self.lanes.values())


# --------------------------------------------------------------------------- #
# fail-soft artifact loaders                                                   #
# --------------------------------------------------------------------------- #


def _load_json(path: Path) -> object | None:
    """Read + parse a JSON artifact, FAIL-SOFT. Missing/torn/garbage -> None (never raises)."""
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 — a missing/torn artifact is a 0/None metric, never a crash
        return None


def _load_reads(cdir: Path) -> list[dict]:
    """``sentiment_reads.json`` -> list of raw read dicts (fail-soft; non-list -> [])."""
    raw = _load_json(cdir / "sentiment_reads.json")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def _load_plans(cdir: Path) -> list[dict]:
    """Decider plans -> list of raw plan dicts. Prefers ``plans.json`` (a list); falls back to
    per-coin ``plan_<COIN>.json`` objects. Fail-soft: any unreadable source contributes nothing."""
    raw = _load_json(cdir / "plans.json")
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    plans: list[dict] = []
    for p in sorted(cdir.glob("plan_*.json")):
        obj = _load_json(p)
        if isinstance(obj, dict):
            plans.append(obj)
    return plans


def _load_auditor(cdir: Path) -> dict | None:
    """``auditor.json`` -> the verdict dict (fail-soft; missing/non-object -> None)."""
    raw = _load_json(cdir / "auditor.json")
    return raw if isinstance(raw, dict) else None


# --------------------------------------------------------------------------- #
# citation resolution (ground truth)                                           #
# --------------------------------------------------------------------------- #


def _resolve_tags(content_dir, item_id: str) -> set[str] | None:
    """Resolve a cited item via the content store (GROUND TRUTH) and return its tagged coin set
    (upper-cased), fail-soft. Returns None when the id does NOT resolve (a fabricated citation)."""
    try:
        it = get_item(content_dir, item_id)
    except Exception:  # noqa: BLE001 — a torn store can never raise into the QA loop
        return None
    if it is None:
        return None
    return {_norm_coin(c) for c in it.coins}


def _claim_coins(claim: dict, read_coin: str) -> list[str]:
    """The coins a claim asserts — its own ``coins`` if any, else the read's coin (mirrors the
    auditor's claim_supports_coin fallback)."""
    coins = [_norm_coin(c) for c in claim.get("coins", []) if str(c).strip()]
    return coins or [_norm_coin(read_coin)]


# --------------------------------------------------------------------------- #
# analyze_cycle                                                                #
# --------------------------------------------------------------------------- #


def analyze_cycle(state_dir, content_dir, cycle: int) -> DecisionQA:
    """Re-derive the per-cycle DECISION-QA snapshot for cycle ``cycle`` from its cycle artifacts.

    FAIL-SOFT throughout: a missing/torn ``sentiment_reads.json`` / ``plans.json`` / auditor file
    simply means the metric it feeds is 0/None — this function NEVER raises, NEVER blocks anything
    (the hard gate is the Auditor's job). Every citation is re-resolved against the content store
    (ground truth): a cited ``item_id`` that does not resolve, OR resolves but does NOT tag the
    claimed coin, counts as one HALLUCINATED citation against the read's lane.
    """
    cdir = cycle_dir(state_dir, cycle)
    reads = _load_reads(cdir)
    plans = _load_plans(cdir)
    auditor = _load_auditor(cdir)

    # ---- per-lane stats + per-(lane,coin) cited item_id sets (for redundancy) ----------------- #
    lanes: dict[str, LaneStats] = {ln: LaneStats(lane=ln) for ln in LANES}
    # cited item_id SETS keyed by (lane, coin) — the redundancy Jaccard works on the resolved-or-not
    # cited ids (echoing the same id is redundancy regardless of whether the id is real).
    cited_by_lane_coin: dict[tuple[str, str], set[str]] = defaultdict(set)
    mislabels: list[str] = []
    # per-lane staked-confidence accumulator for calibration: for each high-confidence (>= 0.6) read
    # we append its confidence if it had ANY hallucinated citation, else 0.0. The lane's penalty is
    # the mean over this list (so betting big AND wrong costs more than hedging).
    staked_conf: dict[str, list[float]] = defaultdict(list)

    for read in reads:
        lane = str(read.get("agent", "")).strip()
        coin = _norm_coin(read.get("coin", ""))
        ls = lanes.get(lane)
        if ls is None:
            # an unknown lane name (schema drift) is tracked under its literal name so its metrics
            # are not silently dropped, but it never participates in the canonical LANES redundancy.
            ls = lanes.setdefault(lane, LaneStats(lane=lane))
        ls.n_reads += 1
        stance = str(read.get("stance", "")).strip()
        if stance and stance != "neutral":
            ls.n_nonneutral += 1

        # s/level round-trip mislabel (re-derived; never trusts the stored pairing).
        try:
            s_val = float(read.get("s"))
            level = read.get("level")
            if s_to_level(s_val) != level:
                mislabels.append(
                    f"{coin}/{lane}: s={s_val} buckets to {s_to_level(s_val)} not level={level}"
                )
        except Exception:  # noqa: BLE001 — a malformed s/level is itself a mislabel, never a crash
            mislabels.append(
                f"{coin}/{lane}: malformed s/level {read.get('s')!r}/{read.get('level')!r}"
            )

        try:
            conf = float(read.get("confidence"))
        except Exception:  # noqa: BLE001
            conf = 0.0
        read_hallucinated = False
        read_grounded = False  # set True by ANY resolving, coin-covering citation below

        claims = read.get("claims", [])
        if not isinstance(claims, list):
            claims = []
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            ls.total_claims += 1
            claim_coins = _claim_coins(claim, coin)
            item_ids = claim.get("item_ids", [])
            if not isinstance(item_ids, list):
                continue
            for iid in item_ids:
                iid = str(iid)
                cited_by_lane_coin[(lane, coin)].add(iid)
                ls.total_citations += 1
                tags = _resolve_tags(content_dir, iid)
                # HALLUCINATED iff the id does not resolve OR resolves but does NOT tag EVERY claimed
                # coin (ALL-semantics, mirroring the Auditor's check 2 `claim_supports_coin`, which
                # flags the citation when ANY claimed coin is missing from the item's tags). Using
                # `not all(...)` — not `not any(...)` — closes the laundering hole where a lane
                # co-tags a real coin Y onto a claim about X and cites only a Y-item.
                if tags is None or not all(c in tags for c in claim_coins):
                    ls.hallucinated_citations += 1
                    read_hallucinated = True
                else:
                    # a citation that resolves AND covers every claimed coin GROUNDS the read.
                    read_grounded = True

        # UNGROUNDED CONVICTION: a NONNEUTRAL read that produced NO resolving coin-matching citation
        # (empty/absent claims, or every cited id hallucinated) is unsupported conviction. Count it
        # as one ungrounded unit so the hallucination_rate is non-zero — a directional read can never
        # earn a perfect grounding score on zero evidence (finding 8). Neutral reads make no
        # conviction claim and are exempt. (A read whose only citations hallucinated already has
        # read_hallucinated set, so it is also a no-grounding directional read.)
        is_ungrounded_conviction = stance and stance != "neutral" and not read_grounded
        if is_ungrounded_conviction:
            ls.ungrounded_nonneutral += 1

        # calibration: only HIGH-conviction reads stake a penalty (a hedged read errs cheaply). An
        # ungrounded directional read (high conviction on no resolving evidence) stakes too.
        if conf >= 0.6:
            bad = read_hallucinated or is_ungrounded_conviction
            staked_conf[lane].append(conf if bad else 0.0)

    for lane, ls in lanes.items():
        staked = staked_conf.get(lane, [])
        ls.calibration_penalty = (sum(staked) / len(staked)) if staked else 0.0

    # ---- lane_redundancy: per-coin Jaccard overlap of cited item_id sets across lanes --------- #
    # For each coin we take each CANONICAL lane's cited-id set and average the pairwise Jaccard over
    # every pair of lanes that BOTH cited something for that coin. A coin touched by < 2 such lanes
    # has no overlap to measure and is skipped (it cannot demonstrate echo/independence).
    redundancy_by_coin: dict[str, float] = {}
    coins_seen: set[str] = {coin for (_ln, coin) in cited_by_lane_coin}
    for coin in coins_seen:
        sets = [
            cited_by_lane_coin[(ln, coin)]
            for ln in LANES
            if cited_by_lane_coin.get((ln, coin))
        ]
        if len(sets) < 2:
            continue
        overlaps: list[float] = []
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                union = sets[i] | sets[j]
                inter = sets[i] & sets[j]
                overlaps.append((len(inter) / len(union)) if union else 0.0)
        if overlaps:
            redundancy_by_coin[coin] = sum(overlaps) / len(overlaps)
    redundancy_mean = (
        sum(redundancy_by_coin.values()) / len(redundancy_by_coin)
        if redundancy_by_coin else 0.0
    )

    # ---- ungrounded_plans: a directional plan with no coin-matching, stance-matching read ----- #
    # Build, per coin, the set of stances asserted by reads citing >= 1 GROUNDED item for that coin
    # (an item that resolves AND tags the coin). A directional plan needs a read whose stance agrees
    # with the plan's direction (long<-bullish, short<-bearish) resting on real evidence.
    grounded_stances: dict[str, set[str]] = defaultdict(set)
    for read in reads:
        coin = _norm_coin(read.get("coin", ""))
        stance = str(read.get("stance", "")).strip()
        claims = read.get("claims", []) if isinstance(read.get("claims"), list) else []
        grounded = False
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            item_ids = claim.get("item_ids", []) if isinstance(claim.get("item_ids"), list) else []
            for iid in item_ids:
                tags = _resolve_tags(content_dir, str(iid))
                if tags is not None and coin in tags:
                    grounded = True
                    break
            if grounded:
                break
        if grounded and stance:
            grounded_stances[coin].add(stance)

    _DIR_TO_STANCE = {"long": "bullish", "short": "bearish"}
    ungrounded_plans: list[str] = []
    for plan in plans:
        rating = plan.get("rating")
        direction = rating_to_direction(rating) if isinstance(rating, str) else None
        if direction is None:
            continue  # 'flat' / unknown rating — no directional decision to ground
        coin = _coin_of(plan.get("symbol", ""))
        want = _DIR_TO_STANCE.get(direction)
        if want not in grounded_stances.get(coin, set()):
            ungrounded_plans.append(
                f"{plan.get('symbol')} rating={rating} ({direction}): no grounded "
                f"{want}-stance read for {coin}"
            )

    # ---- auditor signals (copied from the persisted verdict; all neutral when absent) --------- #
    auditor_passed: bool | None = None
    n_advisories = 0
    n_blocked = 0
    mismatch_checks: dict[str, int] = {}
    advisory_checks: dict[str, int] = {}
    if auditor is not None:
        auditor_passed = auditor.get("passed") if isinstance(auditor.get("passed"), bool) else None
        advisories = auditor.get("advisories")
        advisories = advisories if isinstance(advisories, list) else []
        n_advisories = len(advisories)
        blocked = auditor.get("blocked_proposals")
        n_blocked = len(blocked) if isinstance(blocked, list) else 0
        mismatches = auditor.get("mismatches")
        for name in mismatches if isinstance(mismatches, list) else []:
            mismatch_checks[str(name)] = mismatch_checks.get(str(name), 0) + 1
        # advisory strings are formatted "[check_name] detail" — tally the bracketed check-name.
        for adv in advisories:
            m = re.match(r"\[([^\]]+)\]", str(adv))
            if m:
                advisory_checks[m.group(1)] = advisory_checks.get(m.group(1), 0) + 1

    return DecisionQA(
        cycle=cycle,
        lanes=lanes,
        mislabels=mislabels,
        ungrounded_plans=ungrounded_plans,
        lane_redundancy_mean=redundancy_mean,
        lane_redundancy_by_coin=redundancy_by_coin,
        auditor_passed=auditor_passed,
        n_advisories=n_advisories,
        n_blocked_proposals=n_blocked,
        mismatch_checks=mismatch_checks,
        advisory_checks=advisory_checks,
    )


# --------------------------------------------------------------------------- #
# update_reliability                                                           #
# --------------------------------------------------------------------------- #


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _lane_cycle_score(ls: LaneStats, redundancy_mean: float, calibration_penalty: float) -> float:
    """The per-cycle reliability score in [0, 1] for a lane.

    Blends three deterministic components (all in [0, 1], higher = better):

    * grounding  = 1 - hallucination_rate    (did its citations resolve + tag the coin?)
    * independence = 1 - redundancy_mean      (is the lane corroborating, not echoing the others?)
    * calibration  = 1 - calibration_penalty  (did it stake HIGH confidence on bad reads?)

    A lane that cited NOTHING is neither rewarded nor punished on grounding (rate 0 -> grounding 1).
    The blend weights grounding heaviest (it is the core anti-hallucination signal), then
    calibration, then independence (a shared item set is a soft, cross-lane signal — it nudges,
    it does not dominate). Weights sum to 1 so the score stays in [0, 1]."""
    grounding = 1.0 - ls.hallucination_rate
    independence = 1.0 - _clamp01(redundancy_mean)
    calibration = 1.0 - _clamp01(calibration_penalty)
    score = 0.6 * grounding + 0.25 * calibration + 0.15 * independence
    return _clamp01(score)


def _atomic_write_text(path: Path, text: str) -> None:
    """Temp file + os.replace (the content_store / cycle_io crash-safe pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def update_reliability(memory_dir, qa: DecisionQA, alpha: float = 0.3) -> dict:
    """Fold one cycle's QA into ``memory/agent_reliability.json`` via an EWMA per lane.

    For each CANONICAL lane THAT PRODUCED >= 1 READ this cycle we compute a per-cycle score
    (:func:`_lane_cycle_score`) blending grounding (1 - hallucination_rate), independence
    (1 - redundancy) and calibration, then update the rolling reliability with
    ``new = alpha * score + (1 - alpha) * prior`` (a lane's FIRST observed cycle seeds reliability =
    score). The lane's ``hallucination_rate_ewma`` is tracked the same way so a caller can read the
    smoothed error rate directly.

    A lane that produced NO reads this cycle is SKIPPED (finding 9): it is NEVER credited a clean
    cycle. If it has prior history that record is carried forward UNCHANGED (no EWMA blend, no
    ``n_cycles`` bump); if it has none it is seeded as UNOBSERVED (``n_cycles`` = 0) so its first
    real cycle later SEEDS from that cycle's true score instead of blending against a fabricated
    clean prior. ``n_cycles`` counts only cycles in which the lane actually produced reads.

    The loaded prior reliability / hallucination-rate are CLAMPED to [0, 1] before the blend and the
    blended result is clamped (finding 10), so a corrupt prior file can never push a score out of
    range. Returns the persisted ``{lane: {...}}`` map.

    ATOMIC write (temp + os.replace) and FAIL-SOFT load: a missing/torn prior file is treated as no
    prior history. NEVER raises — the self-improvement loop must not be able to crash a cycle."""
    mem = Path(memory_dir)
    path = mem / "agent_reliability.json"
    prior = _load_json(path)
    prior = prior if isinstance(prior, dict) else {}

    a = _clamp01(alpha)
    out: dict[str, dict] = {}
    # `out` starts from prior so lanes NOT present this cycle keep their last reliability untouched.
    for lane, rec in prior.items():
        if isinstance(rec, dict):
            out[lane] = dict(rec)

    for lane in LANES:
        ls = qa.lanes.get(lane, LaneStats(lane=lane))
        prev = out.get(lane)

        # FINDING 9 — a lane with NO reads this cycle is not credited a clean cycle.
        if ls.n_reads == 0:
            if isinstance(prev, dict) and isinstance(prev.get("reliability"), (int, float)):
                # carry the prior record forward UNCHANGED (no blend, no n_cycles bump).
                continue
            # never observed and no prior: record it as UNOBSERVED so a later real cycle SEEDS fresh
            # (n_cycles = 0 means "no clean cycle has been credited") rather than blending against a
            # fabricated clean score. reliability is left at the neutral default; it carries no
            # demonstrated signal until the lane actually produces reads.
            out[lane] = {
                "reliability": 1.0,
                "n_cycles": 0,
                "hallucination_rate_ewma": 0.0,
                "last_cycle": qa.cycle,
            }
            continue

        hrate = _clamp01(ls.hallucination_rate)
        # calibration penalty is carried on the LaneStats (mean staked confidence on the lane's
        # high-conviction reads that proved hallucinated/ungrounded), so update_reliability is a PURE
        # function of the QA snapshot (offline, no extra I/O).
        score = _lane_cycle_score(ls, qa.lane_redundancy_mean, ls.calibration_penalty)

        # blend against prior ONLY when the lane has a PRIOR OBSERVED cycle (n_cycles >= 1); an
        # unobserved seed (n_cycles == 0) carries no real history, so the first real cycle SEEDS.
        has_history = (
            isinstance(prev, dict)
            and isinstance(prev.get("reliability"), (int, float))
            and int(prev.get("n_cycles", 0) or 0) >= 1
        )
        if has_history:
            prev_rel = _clamp01(float(prev["reliability"]))  # finding 10 — clamp loaded prior
            reliability = _clamp01(a * score + (1.0 - a) * prev_rel)
            prev_hr = prev.get("hallucination_rate_ewma")
            prev_hr = _clamp01(float(prev_hr)) if isinstance(prev_hr, (int, float)) else hrate
            hr_ewma = _clamp01(a * hrate + (1.0 - a) * prev_hr)
            n_cycles = int(prev.get("n_cycles", 0)) + 1
        else:
            reliability = _clamp01(score)  # seed on the lane's first OBSERVED cycle
            hr_ewma = hrate
            n_cycles = 1

        out[lane] = {
            "reliability": reliability,
            "n_cycles": n_cycles,
            "hallucination_rate_ewma": hr_ewma,
            "last_cycle": qa.cycle,
        }

    _atomic_write_text(path, json.dumps(out, indent=2, default=str))
    return out
