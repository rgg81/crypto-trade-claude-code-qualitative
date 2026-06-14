from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

LessonState = Literal["candidate", "validated", "retired"]
# A lesson's directional pull on the desk. 'restrictive' = a brake (do NOT / cut / avoid);
# 'enabling' = an accelerator (DO take / size the trade when X); 'process' = neutral discipline
# (journal a falsifiable prediction, etc.). Used by the retrieval quota so a losing record can't
# flood every debate with prohibitions and talk the desk out of its own edge.
Polarity = Literal["restrictive", "enabling", "process"]


class Lesson(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    ts: datetime
    text: str
    regime: str | None = None         # quadrant it applies to; None = applies in all regimes
    symbol: str | None = None
    tags: list[str] = Field(default_factory=list)
    importance: int = 5               # 1-10
    polarity: Polarity = "restrictive"  # legacy lessons (no field) default to the dominant type
    state: LessonState = "candidate"
    confirmations: int = 0
    provenance: list[str] = Field(default_factory=list)  # journal decision ids


def _store(memory_dir) -> Path:
    return Path(memory_dir) / "lessons" / "lessons.jsonl"


def append_lesson(memory_dir, fields: dict, ts: datetime) -> str:
    data = {**fields, "ts": ts}
    data.setdefault("id", uuid.uuid4().hex)
    lesson = Lesson.model_validate(data)
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(lesson.model_dump_json() + "\n")
    return lesson.id


def read_lessons(memory_dir) -> list[Lesson]:
    p = _store(memory_dir)
    if not p.exists():
        return []
    return [Lesson.model_validate_json(line) for line in p.read_text().splitlines() if line.strip()]


def score_lesson(lesson: Lesson, now: datetime, query_tags: list[str],
                 w_rec: float = 1.0, w_imp: float = 1.0, w_rel: float = 1.0) -> float:
    """Generative-Agents-style score: recency (Ebbinghaus) + importance + tag relevance (Jaccard).
    """
    hours = max(0.0, (now - lesson.ts).total_seconds() / 3600.0)
    recency = 0.995 ** hours
    importance = lesson.importance / 10.0
    qt, lt = set(query_tags), set(lesson.tags)
    relevance = len(qt & lt) / len(qt | lt) if (qt or lt) else 0.0
    return w_rec * recency + w_imp * importance + w_rel * relevance


def _regime_applies(lz_regime: str | None, query) -> bool:
    """A lesson applies when it is UNIVERSAL (regime None or the literal 'any') or its tag matches
    one of the query contexts. cy77/78 fix: `query` may be a single regime string OR an iterable of
    contexts (the symbol QUADRANT 'high_vol_trend' AND the desk ENGINE label 'risk_off'), since the
    corpus mixes both vocabularies — matching only the quadrant stranded the 50 risk_off-tagged edge
    lessons and the 11 'any' lessons on the common path."""
    if lz_regime in (None, "any"):
        return True
    if query is None:
        return False
    accept = {query} if isinstance(query, str) else {q for q in query if q}
    return lz_regime in accept


def retrieve_lessons(memory_dir, now: datetime, regime,
                     query_tags: list[str], k: int = 5,
                     max_restrictive: int = 3) -> list[Lesson]:
    """Regime-filter FIRST (a lesson with regime None/'any' applies everywhere), rank by score, then
    apply a POLARITY QUOTA so the injected set is two-sided: VALIDATED lessons (standing rules)
    are always kept; >=1 enabling lesson is force-included when any exists; and restrictive
    *fills* are capped at `max_restrictive` so a losing record's prohibitions can't monopolize
    every debate. Retired lessons excluded.

    `regime` is the query CONTEXT(S): a single regime string OR an iterable of contexts (pass BOTH
    the symbol quadrant and the desk engine label so risk-state-tagged lessons surface — see
    `_regime_applies`). Passing None matches only universal lessons."""
    candidates = [
        lz for lz in read_lessons(memory_dir)
        if lz.state != "retired" and _regime_applies(lz.regime, regime)
    ]
    candidates.sort(key=lambda lz: score_lesson(lz, now, query_tags), reverse=True)

    validated = [lz for lz in candidates if lz.state == "validated"]
    pool = [lz for lz in candidates if lz.state != "validated"]
    out: list[Lesson] = list(validated)  # standing rules are never dropped by the quota

    # Force-include the highest-scored enabling lesson if none is in the set yet.
    if len(out) < k and not any(lz.polarity == "enabling" for lz in out):
        enabling = next((lz for lz in pool if lz.polarity == "enabling"), None)
        if enabling is not None:
            out.append(enabling)

    # Fill the remaining slots by score, capping restrictive FILLS (validated already counted).
    n_restrict = 0
    for lz in pool:
        if lz in out:
            continue
        if len(out) >= k:
            break
        if lz.polarity == "restrictive" and n_restrict >= max_restrictive:
            continue  # don't flood the debate with prohibitions
        out.append(lz)
        if lz.polarity == "restrictive":
            n_restrict += 1

    out.sort(key=lambda lz: score_lesson(lz, now, query_tags), reverse=True)
    return out[:max(k, len(validated))]  # never truncate away a validated standing rule


def _write_all(memory_dir, lessons: list[Lesson]) -> None:
    p = _store(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(lz.model_dump_json() + "\n" for lz in lessons))


def update_lesson(memory_dir, lesson_id: str, **fields) -> bool:
    """Merge `fields` into the lesson with `lesson_id`; rewrites the store. False if not found."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            lessons[i] = lz.model_copy(update=fields)
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def confirm_lesson(memory_dir, lesson_id: str, *, promote_threshold: int = 5) -> bool:
    """Increment a lesson's confirmation count; promote CANDIDATE -> VALIDATED at the threshold.
    (Count-based here; Phase C gates promotion additionally on statistical support — spec §6.)"""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            c = lz.confirmations + 1
            state = (
                "validated"
                if (lz.state == "candidate" and c >= promote_threshold)
                else lz.state
            )
            lessons[i] = lz.model_copy(update={"confirmations": c, "state": state})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def demote_lesson(memory_dir, lesson_id: str) -> bool:
    """Step a lesson down: VALIDATED -> CANDIDATE, CANDIDATE/RETIRED -> RETIRED.
    Used to aggressively age out stale or regime-mismatched rules (spec §6).
    Resets confirmations to 0 so a demoted lesson must re-earn promotion
    (anti-ossification, spec §6)."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            new = "candidate" if lz.state == "validated" else "retired"
            lessons[i] = lz.model_copy(update={"state": new, "confirmations": 0})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit


def retire_lesson(memory_dir, lesson_id: str) -> bool:
    return update_lesson(memory_dir, lesson_id, state="retired")


def validated_lessons(memory_dir) -> list[Lesson]:
    """The VALIDATED lessons — these act as hard vetoes / standing rules for the team."""
    return [lz for lz in read_lessons(memory_dir) if lz.state == "validated"]


def statistically_promote(memory_dir, lesson_id: str, *, dsr_pvalue: float,
                          promote_threshold: int = 5, dsr_threshold: float = 0.95) -> bool:
    """Confirm a lesson, but only allow CANDIDATE->VALIDATED promotion when the desk's edge is
    statistically proven (DSR p-value >= threshold). Below the gate the confirmation still
    counts, but the lesson stays CANDIDATE — the statistical layer over B3's count-based rule
    (spec §6). Returns True if the lesson was found."""
    lessons = read_lessons(memory_dir)
    hit = False
    for i, lz in enumerate(lessons):
        if lz.id == lesson_id:
            c = lz.confirmations + 1
            promote = (lz.state == "candidate" and c >= promote_threshold
                       and dsr_pvalue >= dsr_threshold)
            lessons[i] = lz.model_copy(update={"confirmations": c,
                                               "state": "validated" if promote else lz.state})
            hit = True
    if hit:
        _write_all(memory_dir, lessons)
    return hit
