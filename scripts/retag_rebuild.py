"""Re-tag and rebuild the LIVE content store with the now-FIXED tagging + dedup.

A quality audit found the intake layer was corrupting the crowd read: the old tagger matched coin
tickers as bare substrings (so "linked"/"blink" false-tagged LINK and "tether"/"ethena"
false-tagged ETH), and the forums adapter mirrored the shared news RSS feeds so the SAME article was
double-counted under two source names ("rss" and "forums"). The intake fixes have landed in
:func:`futures_fund.vendors.tag_instruments` (word-boundary tagging) and
:func:`futures_fund.content_store.dedup_key` (canonical-url cross-source collapse) — but the items
already on disk were written under the OLD rules. This one-shot maintenance pass re-runs the fixed
intake over the existing store so the digests the desk reads are clean.

For every ``content/items/items-YYYY-MM-DD.jsonl`` it:

  1. Re-runs the FIXED :func:`tag_instruments` over each item's ``title + body`` to recompute
     ``item.coins`` (drops the false tags; never invents a coin outside the working symbol set).
  2. COLLAPSES cross-source duplicates that share a canonical url (the forums≈rss mirror), keeping
     the RICHEST (longer-body) copy and PRESERVING any human/LLM ``summary`` / ``item_sentiment``
     carried by either copy (so a dedup never throws away an already-summarized verdict).
  3. Rewrites each day file ATOMICALLY (temp file + ``os.replace``), repartitioned by published-day.
  4. REBUILDS ``content/index/by_coin/*.jsonl`` from scratch (atomic) — one pointer per tagged coin.
  5. RECOMPUTES every ``content/digests/<COIN>.json`` at an injected ``now`` (``--now`` if given,
     else the newest item's ``fetched_ts``), so ``rolling_s`` / ``source_breakdown`` are clean.

It is IDEMPOTENT (a second run over an already-clean store is a no-op in content) and crash-safe
(all writes are temp-then-replace). Time is INJECTED so the rebuild is deterministic and offline.

    uv run python scripts/retag_rebuild.py
    uv run python scripts/retag_rebuild.py --content-dir content --config config.yaml
    uv run python scripts/retag_rebuild.py --now 2026-06-13T23:00:00Z
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

from futures_fund.config import load_settings
from futures_fund.content_store import (
    ContentItem,
    _as_utc,
    _atomic_write_text,
    _coin_index_path,
    _day_file,
    _day_key,
    _digest_path,
    _digests_dir,
    _index_dir,
    _items_dir,
    _pointer,
    _read_jsonl,
    dedup_key,
    update_digest,
)
from futures_fund.vendors import tag_instruments


def _symbol_universe(content_dir: str, config_path: str) -> list[str]:
    """The working symbol set the re-tag runs against: the config ``core_watchlist`` UNION any coin
    already tagged in the store (so a coin outside the watchlist that the crawler once added is not
    silently dropped because the re-tag forgot it). Order: watchlist first, then extras sorted."""
    try:
        watchlist = list(load_settings(config_path).qualitative.core_watchlist)
    except Exception:  # noqa: BLE001 — a missing/garbage config falls back to whatever the store holds
        watchlist = []
    seen = {s.upper() for s in watchlist}
    extras: set[str] = set()
    items_dir = _items_dir(content_dir)
    if items_dir.exists():
        for path in sorted(items_dir.glob("items-*.jsonl")):
            for rec in _read_jsonl(path):
                for coin in rec.get("coins", []) or []:
                    cu = str(coin).upper()
                    if cu not in seen:
                        seen.add(cu)
                        extras.add(cu)
    return watchlist + sorted(extras)


def _retag(item: ContentItem, symbols: list[str]) -> ContentItem:
    """Recompute ``coins`` with the FIXED word-boundary tagger over title + body, dropping the old
    substring false-tags. Returns a copy; everything else (id/source/sentiment/summary) is kept."""
    text = f"{item.title or ''} {item.body or ''}"
    return item.model_copy(update={"coins": tag_instruments(text, symbols)})


def _richer(a: ContentItem, b: ContentItem) -> ContentItem:
    """The copy to KEEP when ``a`` and ``b`` collapse to one dedup_key: the longer body wins; ties
    break on the lexically-smaller id so the choice is DETERMINISTIC (idempotent across runs)."""
    la, lb = len(a.body or ""), len(b.body or "")
    if la != lb:
        return a if la > lb else b
    return a if a.id <= b.id else b


def _merge_dup(keep: ContentItem, drop: ContentItem) -> ContentItem:
    """Fold a dropped duplicate's analyst verdict into the kept copy so a collapse never loses an
    already-summarized ``summary`` / ``item_sentiment`` / ``summarized_ts`` that only the dropped
    copy carried. The kept copy's own values win when present; else inherit the dropped copy's."""
    updates: dict = {}
    if keep.item_sentiment is None and drop.item_sentiment is not None:
        updates["item_sentiment"] = drop.item_sentiment
    if not keep.summary and drop.summary:
        updates["summary"] = drop.summary
    if keep.summarized_ts is None and drop.summarized_ts is not None:
        updates["summarized_ts"] = drop.summarized_ts
    return keep.model_copy(update=updates) if updates else keep


def _collapse(items: list[ContentItem]) -> list[ContentItem]:
    """Collapse cross-source canonical-url duplicates (and url-less same-source title repeats) by
    :func:`dedup_key`, keeping the richest copy and merging in any sibling's verdict. First-seen
    order is preserved for the survivors so the rewrite is stable."""
    chosen: dict[str, ContentItem] = {}
    order: list[str] = []
    for it in items:
        key = dedup_key(it.source, it.url, it.title)
        prev = chosen.get(key)
        if prev is None:
            chosen[key] = it
            order.append(key)
            continue
        keep = _richer(prev, it)
        drop = it if keep is prev else prev
        chosen[key] = _merge_dup(keep, drop)
    return [chosen[k] for k in order]


def _load_store(content_dir: str) -> list[ContentItem]:
    """Every item in the store, in (day-file, line) order. Unparseable lines are skipped by
    :func:`_read_jsonl`; a row that fails ContentItem validation is skipped too (never crash)."""
    items: list[ContentItem] = []
    items_dir = _items_dir(content_dir)
    if not items_dir.exists():
        return items
    for path in sorted(items_dir.glob("items-*.jsonl")):
        for rec in _read_jsonl(path):
            if not rec.get("id"):
                continue
            try:
                items.append(ContentItem.model_validate(rec))
            except Exception:  # noqa: BLE001 — a malformed record is dropped, not fatal
                continue
    return items


def _resolve_now(items: list[ContentItem], now_arg: str | None) -> datetime:
    """The digest clock: ``--now`` (ISO) if given, else the NEWEST item's ``fetched_ts``, else real
    now. ``Z`` is accepted as a UTC suffix."""
    if now_arg:
        return _as_utc(datetime.fromisoformat(now_arg.replace("Z", "+00:00")))
    if items:
        return _as_utc(max(_as_utc(it.fetched_ts) for it in items))
    return datetime.now(UTC)


def rebuild(content_dir: str, config_path: str = "config.yaml", now_arg: str | None = None) -> dict:
    """Re-tag + dedup + rewrite day files + rebuild the index + recompute every digest. Returns a
    summary dict (counts + per-coin before/after deltas) for the CLI to print."""
    symbols = _symbol_universe(content_dir, config_path)

    raw = _load_store(content_dir)
    now = _resolve_now(raw, now_arg)

    # --- BEFORE snapshot (read straight off the existing digests on disk) ---
    before_digests = _snapshot_digests(content_dir)

    # --- re-tag, then collapse cross-source dups ---
    retagged = [_retag(it, symbols) for it in raw]
    collapsed = _collapse(retagged)

    # --- repartition by published-day, rewrite each day file atomically ---
    by_day: dict[str, list[ContentItem]] = {}
    for it in collapsed:
        by_day.setdefault(_day_key(it.published_ts), []).append(it)

    items_dir = _items_dir(content_dir)
    # day files that no longer have any rows are deleted below (keep the store honest)
    existing_day_files = set(items_dir.glob("items-*.jsonl")) if items_dir.exists() else set()
    for day, rows in by_day.items():
        text = "".join(
            json.dumps(json.loads(it.model_dump_json()), default=str) + "\n" for it in rows
        )
        _atomic_write_text(_day_file(content_dir, day), text)
    kept_day_files = {_day_file(content_dir, day) for day in by_day}
    for stale in existing_day_files - kept_day_files:
        stale.unlink()

    # --- rebuild the per-coin pointer index from scratch (atomic, one pointer per tagged coin) ---
    index_dir = _index_dir(content_dir)
    existing_index_files = set(index_dir.glob("*.jsonl")) if index_dir.exists() else set()
    pointers_by_coin: dict[str, list[dict]] = {}
    for it in collapsed:
        day_file = f"items-{_day_key(it.published_ts)}.jsonl"
        seen_coins: set[str] = set()
        for coin in it.coins:
            ckey = coin.upper()
            if ckey in seen_coins:
                continue
            seen_coins.add(ckey)
            pointers_by_coin.setdefault(ckey, []).append(_pointer(it, day_file))
    for coin, pointers in pointers_by_coin.items():
        text = "".join(json.dumps(p, default=str) + "\n" for p in pointers)
        _atomic_write_text(_coin_index_path(content_dir, coin), text)
    # drop index files for coins that no longer have ANY pointer after the re-tag
    kept_index_files = {_coin_index_path(content_dir, c) for c in pointers_by_coin}
    for stale in existing_index_files - kept_index_files:
        stale.unlink()

    # --- recompute every digest: the union of coins that have (a) an index now, (b) a digest file
    #     already, (c) a tag in the rebuilt store. Coins that lost ALL their items get their stale
    #     digest removed (no orphan claiming a dead coin still trends). ---
    digests_dir = _digests_dir(content_dir)
    existing_digest_coins = {
        p.stem.upper() for p in digests_dir.glob("*.json")
    } if digests_dir.exists() else set()
    coins_with_items = set(pointers_by_coin)
    after_digests: dict[str, dict] = {}
    for coin in sorted(coins_with_items):
        after_digests[coin] = update_digest(content_dir, coin, now)
    for dead in existing_digest_coins - coins_with_items:
        _digest_path(content_dir, dead).unlink()

    return {
        "now": now.isoformat(),
        "symbols": symbols,
        "items_before": len(raw),
        "items_after": len(collapsed),
        "collapsed": len(raw) - len(collapsed),
        "before": before_digests,
        "after": after_digests,
    }


def _snapshot_digests(content_dir: str) -> dict[str, dict]:
    """The existing on-disk digests keyed by coin (the BEFORE picture). Empty if none exist yet."""
    out: dict[str, dict] = {}
    digests_dir = _digests_dir(content_dir)
    if not digests_dir.exists():
        return out
    for path in sorted(digests_dir.glob("*.json")):
        try:
            out[path.stem.upper()] = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
    return out


def _fmt_delta(coin: str, before: dict | None, after: dict | None) -> str:
    """One before/after line for a coin's digest (n_items_30d, rolling_s, source_breakdown)."""
    def g(d, k, default):
        return d.get(k, default) if isinstance(d, dict) else default
    b_n = g(before, "n_items_30d", "-")
    a_n = g(after, "n_items_30d", "-" if after is None else 0)
    b_s = g(before, "rolling_s", None)
    a_s = g(after, "rolling_s", None)
    b_ss = f"{b_s:.3f}" if isinstance(b_s, (int, float)) else "-"
    a_ss = f"{a_s:.3f}" if isinstance(a_s, (int, float)) else "-"
    b_sb = g(before, "source_breakdown", {})
    a_sb = g(after, "source_breakdown", {})
    return (
        f"  {coin:5} n_items {b_n!s:>4} -> {a_n!s:<4}  rolling_s {b_ss:>8} -> {a_ss:<8}"
        f"  src {b_sb} -> {a_sb}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Re-tag + dedup + rebuild the content store (index + digests)."
    )
    parser.add_argument("--content-dir", default="content")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--now", default=None, help="ISO digest clock; default newest fetched_ts.")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        out = rebuild(args.content_dir, config_path=args.config, now_arg=args.now)
    except Exception as e:  # noqa: BLE001 — a hard failure: surface it, exit non-zero
        print(f"RETAG/REBUILD ERROR: {e!r}")
        return 1

    print(
        f"retag/rebuild ok: now={out['now']} items {out['items_before']} -> {out['items_after']} "
        f"(collapsed {out['collapsed']} cross-source dups) symbols={out['symbols']}"
    )
    coins = sorted(set(out["before"]) | set(out["after"]))
    print("per-coin digest deltas (n_items_30d, rolling_s, source_breakdown):")
    for coin in coins:
        print(_fmt_delta(coin, out["before"].get(coin), out["after"].get(coin)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
