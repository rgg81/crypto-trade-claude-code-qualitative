"""Fold the Sonnet summarizer's output back into the content store — the ONLY writer of summaries.

The LLM summarizer (dispatched by SKILL, never here) reads ``content/_pending_summaries.json`` and
emits a JSON array of ``{item_id, summary, item_sentiment}`` verdicts. The LLM NEVER touches the
store directly: it hands its output to this deterministic CLI, which is the single writer that

  * looks each item up by id, sets ``item.summary`` + ``item.item_sentiment`` + ``summarized_ts``
    (injected ``now``), rewriting its day file ATOMICALLY (temp + os.replace) — and refreshes the
    per-coin index pointers so they carry the new sentiment;
  * VALIDATES ``item_sentiment`` against :data:`~futures_fund.sentiment_decay.SentimentLevel`
    (the five ordinal levels) and SKIPS + logs any invalid label rather than poisoning the store;
  * after all valid verdicts are applied, calls :func:`content_store.update_digest` ONCE per
    affected coin so the rolling decayed-sentiment digests reflect the new verdicts.

Skips (and reports) unknown ids, items already summarized, and malformed rows. Prints a one-line
summary; exit 0 when the file was processed (even with per-row skips), 1 on a hard failure (bad
file / bad JSON shape). Deterministic + offline — ``now`` is injectable.

    uv run python scripts/summarize_apply_cli.py summaries.json
    uv run python scripts/summarize_apply_cli.py summaries.json --content-dir content
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import get_args

from futures_fund import content_store
from futures_fund.content_store import (
    ContentItem,
    _as_utc,
    _atomic_write_text,
    _coin_index_path,
    _day_file,
    _day_key,
    _read_jsonl,
)
from futures_fund.sentiment_decay import SentimentLevel

_VALID_LEVELS: frozenset[str] = frozenset(get_args(SentimentLevel))


def _patch_day_file(content_dir, item: ContentItem) -> bool:
    """Rewrite ``item``'s day file with the updated record (summary/sentiment/summarized_ts).

    Atomic (temp + os.replace), single-writer — matches the content_store day-file contract.
    Returns True iff the item's row was found and replaced in its day file."""
    day = _day_key(item.published_ts)
    path = _day_file(content_dir, day)
    rows = _read_jsonl(path)
    found = False
    out: list[dict] = []
    for rec in rows:
        if rec.get("id") == item.id:
            out.append(json.loads(item.model_dump_json()))
            found = True
        else:
            out.append(rec)
    if not found:
        return False
    _atomic_write_text(path, "".join(json.dumps(r, default=str) + "\n" for r in out))
    return True


def _patch_index_pointers(content_dir, item: ContentItem) -> None:
    """Refresh ``item_sentiment`` on the item's pointers in each tagged coin's index.

    The pointer carries a sentiment copy (for cheap index-only filtering); keep it in sync with the
    canonical day-file record. Atomic per index file; only rewrites files that actually change."""
    for coin in {c.upper() for c in item.coins}:
        path = _coin_index_path(content_dir, coin)
        ptrs = _read_jsonl(path)
        changed = False
        for ptr in ptrs:
            if ptr.get("item_id") == item.id and ptr.get("item_sentiment") != item.item_sentiment:
                ptr["item_sentiment"] = item.item_sentiment
                changed = True
        if changed:
            _atomic_write_text(path, "".join(json.dumps(p, default=str) + "\n" for p in ptrs))


def apply_summaries(content_dir, rows, now: datetime) -> dict:
    """Fold a list of ``{item_id, summary, item_sentiment}`` verdicts into the store.

    Returns ``{applied, skipped, errors, coins_updated}`` where ``errors`` is a list of human
    strings (one per skipped row, with the reason). Validates the sentiment label, applies each
    valid verdict to its item (day file + index pointers), then updates the digest of every touched
    coin EXACTLY ONCE. Items already summarized are skipped (idempotent re-runs are safe)."""
    applied = 0
    skipped = 0
    errors: list[str] = []
    touched_coins: set[str] = set()

    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            skipped += 1
            errors.append(f"row {i}: not an object")
            continue
        iid = row.get("item_id")
        level = row.get("item_sentiment")
        summary = row.get("summary")
        if not iid or not isinstance(iid, str):
            skipped += 1
            errors.append(f"row {i}: missing/invalid item_id")
            continue
        if level not in _VALID_LEVELS:
            skipped += 1
            errors.append(f"row {i} ({iid}): invalid item_sentiment {level!r}; "
                          f"expected one of {sorted(_VALID_LEVELS)}")
            continue
        item = content_store.get_item(content_dir, iid)
        if item is None:
            skipped += 1
            errors.append(f"row {i} ({iid}): unknown item_id")
            continue
        if item.item_sentiment is not None:
            skipped += 1
            errors.append(f"row {i} ({iid}): already summarized; left intact")
            continue

        item.summary = summary if isinstance(summary, str) else (str(summary) if summary else "")
        item.item_sentiment = level  # validated above
        item.summarized_ts = _as_utc(now)
        if not _patch_day_file(content_dir, item):
            skipped += 1
            errors.append(f"row {i} ({iid}): item not in its day file; not applied")
            continue
        _patch_index_pointers(content_dir, item)
        applied += 1
        for coin in item.coins:
            touched_coins.add(coin.upper())

    for coin in sorted(touched_coins):
        content_store.update_digest(content_dir, coin, now)

    return {
        "applied": applied,
        "skipped": skipped,
        "errors": errors,
        "coins_updated": sorted(touched_coins),
    }


def _load_rows(summary_path: str) -> list:
    """Read + shape-check the summarizer output file. Raises ValueError on a non-list shape."""
    raw = json.loads(open(summary_path).read())
    if isinstance(raw, dict) and isinstance(raw.get("summaries"), list):
        raw = raw["summaries"]  # tolerate a {"summaries": [...]} wrapper
    if not isinstance(raw, list):
        raise ValueError("summarizer output must be a JSON array of {item_id, summary, "
                         "item_sentiment} objects")
    return raw


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministically fold the summarizer's output into the content store.")
    parser.add_argument("summary_file", help="JSON file: [{item_id, summary, item_sentiment}, ...]")
    parser.add_argument("--content-dir", default="content")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        rows = _load_rows(args.summary_file)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"APPLY ERROR: {e}")
        return 1

    out = apply_summaries(args.content_dir, rows, datetime.now(UTC))
    print(
        f"summaries applied={out['applied']} skipped={out['skipped']} "
        f"coins_updated={len(out['coins_updated'])}"
        + (f"; first skip: {out['errors'][0]}" if out["errors"] else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
