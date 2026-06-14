"""The 30-day content store — the heart of the qualitative sentiment desk.

Fetched news/social CONTENT (not just an index number) lands here as :class:`ContentItem`s,
partitioned by published-date into day files (``content/items/items-YYYY-MM-DD.jsonl``), with a
per-coin pointer index (``content/index/by_coin/<COIN>.jsonl``) so the desk can pull a coin's
recent items cheaply, and a rolling decayed-sentiment digest per coin
(``content/digests/<COIN>.json``). A nightly :func:`purge` evicts items older than the retention
window so the store stays bounded at ~30 days.

The index and digests are written ATOMICALLY (temp file + ``os.replace`` — the crash-safe
``state.py``/``cycle_io.py`` pattern), so a reader of those never sees a half-written file. The
hot-path JSONL day-file and index APPENDS (dedupe by id) are NOT atomic and assume a SINGLE writer:
they are best-effort append-only and self-healing — a torn trailing fragment from a crashed write
is closed off onto its own (garbage, tolerated) line by the next append so it never corrupts a
following valid record, and :func:`_read_jsonl` skips any unparseable line. Concurrent appenders to
the same day/index file are NOT supported (their writes may interleave).

Time is always INJECTED (`now`) — there is no global clock — so the store is deterministic and
trivially testable offline.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.sentiment_decay import (
    SentimentLevel,
    decay_score,
    level_to_s,
)


class ContentItem(BaseModel):
    """One piece of fetched content (a headline, an article, a social post).

    `published_ts` is when the SOURCE published it (drives partitioning + decay); `fetched_ts` is
    when WE pulled it. `item_sentiment` is the analyst's ordinal verdict (None until summarized).
    """

    id: str
    source: str
    feed: str
    url: str
    title: str
    body: str = ""
    author: str = ""
    published_ts: datetime
    fetched_ts: datetime
    coins: list[str] = Field(default_factory=list)
    engagement: dict = Field(default_factory=dict)
    summary: str | None = None
    item_sentiment: SentimentLevel | None = None
    summarized_ts: datetime | None = None


# --------------------------------------------------------------------------- #
# id / canonicalisation                                                        #
# --------------------------------------------------------------------------- #

_WS_RE = re.compile(r"\s+")
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "ref_src")


def canonical_url(url: str) -> str:
    """Strip the query string (and trailing slash) so the same article reached by different
    tracking params (?utm_source=…) collapses to one id. Empty/garbage url -> "".

    Only the scheme+host+path survive — the entire query and fragment are dropped, which is the
    aggressive-but-safe choice for news/social permalinks (their identity lives in the path)."""
    if not url:
        return ""
    s = str(url).strip()
    # drop fragment, then query
    s = s.split("#", 1)[0]
    s = s.split("?", 1)[0]
    return s.rstrip("/")


def normalize_title(title: str) -> str:
    """Lowercase + collapse internal whitespace so a cross-source repost of the same headline
    (different casing / spacing) collapses to one id."""
    if not title:
        return ""
    return _WS_RE.sub(" ", str(title)).strip().lower()


def make_id(source: str, url: str, title: str) -> str:
    """Stable sha1 identity for an item.

    Keying on source + canonical_url + normalized_title means: the SAME url reposted with tracking
    params dedupes, and (when url is absent/empty) two sources carrying the identical headline ALSO
    collapse via the normalized title. Source is in the key so a genuine cross-source re-report with
    its own permalink stays distinct unless it has no url."""
    raw = f"{source}|{canonical_url(url)}|{normalize_title(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def dedup_key(source: str, url: str, title: str) -> str:
    """The collapse key :func:`store_items` dedupes on (distinct from the item `id`).

    When a canonical url is present, the key is the canonical url ALONE — so the SAME article
    arriving under two source names (e.g. "rss" and "forums") collapses to ONE item regardless of
    source, instead of double-counting the same story in the crowd read. When no url is present we
    fall back to ``source|normalized_title`` (today's behaviour): a cross-source repost with no
    permalink only collapses when the headline matches within the same source. Returned as a sha1
    hex so it matches the `id` shape used as a dedupe handle on disk."""
    cu = canonical_url(url)
    raw = cu if cu else f"{source}|{normalize_title(title)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# paths + atomic write                                                         #
# --------------------------------------------------------------------------- #


def _items_dir(content_dir) -> Path:
    return Path(content_dir) / "items"


def _index_dir(content_dir) -> Path:
    return Path(content_dir) / "index" / "by_coin"


def _digests_dir(content_dir) -> Path:
    return Path(content_dir) / "digests"


def _day_file(content_dir, day: str) -> Path:
    return _items_dir(content_dir) / f"items-{day}.jsonl"


def _coin_index_path(content_dir, coin: str) -> Path:
    return _index_dir(content_dir) / f"{coin.upper()}.jsonl"


def _digest_path(content_dir, coin: str) -> Path:
    return _digests_dir(content_dir) / f"{coin.upper()}.json"


def _as_utc(dt: datetime) -> datetime:
    """Naive datetime is assumed UTC; aware is normalised to UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _day_key(dt: datetime) -> str:
    """The UTC calendar-day partition key (YYYY-MM-DD) an item belongs to."""
    return _as_utc(dt).date().isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + os.replace (atomic rename) — crash mid-write leaves the PRIOR file
    intact (the state.py / cycle_io.py pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # tolerate a torn/garbage line — never crash a read
    return out


def _ends_with_newline(path: Path) -> bool:
    """True iff the file is empty/absent or its last byte is a newline.

    A non-newline last byte means the prior append left a TORN trailing fragment (a crash mid-write,
    or a writer that died before flushing the ``\\n``). Appending onto it would concatenate the new
    record onto the fragment and corrupt BOTH — so the next append must start on a fresh line."""
    try:
        with path.open("rb") as fh:
            try:
                fh.seek(-1, os.SEEK_END)
            except OSError:
                return True  # empty file — nothing to tear
            return fh.read(1) == b"\n"
    except FileNotFoundError:
        return True


def _append_jsonl(path: Path, records: list[dict]) -> None:
    """Append-only write of new JSONL rows (the hot path; dedupe is done by the caller).

    Self-healing against a TORN trailing line: if the file does not already end in a newline, we
    prepend one so a previous crash-truncated fragment is left as its OWN (garbage, tolerated) line
    instead of being concatenated onto — and corrupting — the first new record. The whole batch is
    serialised then written in a SINGLE ``write()`` to shrink the interleave window. (This is the
    hot append path; the index/digest rewrites remain fully atomic via :func:`_atomic_write_text`.)
    """
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = "".join(json.dumps(rec, default=str) + "\n" for rec in records)
    if not _ends_with_newline(path):
        buf = "\n" + buf  # close off a torn fragment so new records start on a clean line
    with path.open("a") as fh:
        fh.write(buf)


# --------------------------------------------------------------------------- #
# store / read                                                                 #
# --------------------------------------------------------------------------- #


def _pointer(item: ContentItem, day_file: str) -> dict:
    return {
        "item_id": item.id,
        "day_file": day_file,
        "published_ts": _as_utc(item.published_ts).isoformat(),
        "source": item.source,
        "item_sentiment": item.item_sentiment,
        "coins": list(item.coins),
    }


def store_items(content_dir, items: list[ContentItem]) -> list[ContentItem]:
    """Dedupe + persist `items`, returning ONLY the newly-stored ones.

    Dedupe is by :func:`dedup_key` against (a) the items already present in the day file the item
    partitions into, and (b) keys already seen WITHIN this batch — so same-url reposts, the SAME
    canonical url under DIFFERENT sources (e.g. "rss" vs "forums"), and (url-less) cross-source
    duplicate titles all collapse, whether they arrive in one batch or across calls. When a batch
    carries two copies that share a dedup_key, the RICHEST (longer-body) copy is the one kept. New
    items are APPENDED to ``items/items-YYYY-MM-DD.jsonl`` (partitioned by published_ts UTC date)
    and a pointer is appended to ``index/by_coin/<COIN>.jsonl`` for every tagged coin.
    """
    # cache of dedup_keys already on disk, per day file we touch (read once)
    day_seen: dict[str, set[str]] = {}
    # the chosen (richest) item per dedup_key seen so far in THIS batch, in first-seen order
    chosen: dict[str, ContentItem] = {}
    order: list[str] = []

    def _day_seen_for(day: str) -> set[str]:
        if day not in day_seen:
            existing = _read_jsonl(_day_file(content_dir, day))
            day_seen[day] = {
                dedup_key(r.get("source", ""), r.get("url", ""), r.get("title", ""))
                for r in existing
                if r.get("id")
            }
        return day_seen[day]

    for item in items:
        key = dedup_key(item.source, item.url, item.title)
        day = _day_key(item.published_ts)
        if key in _day_seen_for(day):
            continue  # already persisted (this call or a prior one)
        if key in chosen:
            # same article twice in this batch — keep the richer (longer-body) copy.
            if len(item.body or "") > len(chosen[key].body or ""):
                chosen[key] = item
            continue
        chosen[key] = item
        order.append(key)

    # accumulate new rows per day file / coin so we write each file once
    new_rows: dict[str, list[dict]] = {}
    new_pointers: dict[str, list[dict]] = {}
    new_items: list[ContentItem] = []

    for key in order:
        item = chosen[key]
        day = _day_key(item.published_ts)
        day_seen[day].add(key)
        new_items.append(item)
        day_file = f"items-{day}.jsonl"
        new_rows.setdefault(day, []).append(json.loads(item.model_dump_json()))
        # De-dupe coins per item: a coin tagged twice (coins=['BTC','BTC'] or mixed case) must write
        # ONE pointer, else update_digest double-counts it in mention_volume / n_items_30d. Case-fold
        # to the canonical upper-case index key and keep first-seen order.
        seen_coins: set[str] = set()
        for coin in item.coins:
            ckey = coin.upper()
            if ckey in seen_coins:
                continue
            seen_coins.add(ckey)
            new_pointers.setdefault(ckey, []).append(_pointer(item, day_file))

    for day, rows in new_rows.items():
        _append_jsonl(_day_file(content_dir, day), rows)
    for coin, pointers in new_pointers.items():
        _append_jsonl(_coin_index_path(content_dir, coin), pointers)

    return new_items


def get_item(content_dir, item_id: str) -> ContentItem | None:
    """Look up a stored item by id (used by the Auditor for point-in-time replay).

    Scans the day files (newest first) — the day partition keeps each file small, and a typical
    lookup hits a recent day. Returns None if the id is unknown."""
    items_dir = _items_dir(content_dir)
    if not items_dir.exists():
        return None
    # newest day file first — auditor lookups skew recent
    for path in sorted(items_dir.glob("items-*.jsonl"), reverse=True):
        for rec in _read_jsonl(path):
            if rec.get("id") == item_id:
                return ContentItem.model_validate(rec)
    return None


def index_for_coin(content_dir, coin: str, since: datetime) -> list[dict]:
    """Return the coin's pointer dicts with ``published_ts >= since`` (inclusive).

    Pointers are cheap (no body) — the desk filters here, then pulls full items via get_item only
    for the ones it needs."""
    since_utc = _as_utc(since)
    out: list[dict] = []
    for ptr in _read_jsonl(_coin_index_path(content_dir, coin)):
        pub = ptr.get("published_ts")
        if not pub:
            continue
        try:
            pub_dt = _as_utc(datetime.fromisoformat(pub))
        except ValueError:
            continue
        if pub_dt >= since_utc:
            out.append(ptr)
    return out


# --------------------------------------------------------------------------- #
# digest                                                                       #
# --------------------------------------------------------------------------- #


def _engagement_weight(item: ContentItem) -> float:
    """A scalar attention proxy from the engagement dict (upvotes/score/comments/votes).

    Higher = more crowd attention. Missing/garbage -> 0.0. Used only to rank `top_item_ids`."""
    eng = item.engagement or {}
    total = 0.0
    for key in ("score", "upvotes", "votes_positive", "num_comments", "comments",
                "likes", "reposts", "engagement"):
        v = eng.get(key)
        if isinstance(v, (int, float)):
            total += float(v)
    return total


def update_digest(content_dir, coin: str, now: datetime, half_life_days: float = 2.5) -> dict:
    """Recompute and persist ``digests/<COIN>.json`` from the coin's last-30d items.

    `rolling_s` is the sum over the coin's items of the AGE-DECAYED sentiment score
    (decay_score(level_to_s(item_sentiment), age_hours, half_life_days)) — items with no
    item_sentiment contribute 0. `mention_volume_24h` counts items published in the trailing 24h;
    `mention_volume_baseline` is the average daily mention count over the ACTUAL span of stored data
    in the window — dividing by the full 30 days when the store only holds a few days would deflate
    the baseline ~6x and make every coin "spike". `top_item_ids` ranks by DECAY-WEIGHTED engagement
    (engagement faded by the same half-life), so a stale viral item demotes once its catalyst is
    priced in. The file is written atomically; `one_line` is left blank for a downstream narrator to
    fill.
    """
    now_utc = _as_utc(now)
    window_days = 30
    window_start = now_utc - _days(window_days)
    day24_start = now_utc - _days(1)

    # gather the coin's items in-window via the pointer index, then hydrate
    items: list[ContentItem] = []
    for ptr in index_for_coin(content_dir, coin, window_start):
        it = get_item(content_dir, ptr.get("item_id"))
        if it is not None:
            items.append(it)

    rolling_s = 0.0
    source_breakdown: dict[str, int] = {}
    mention_volume_24h = 0
    oldest_age_days = 0.0
    for it in items:
        pub = _as_utc(it.published_ts)
        if it.item_sentiment is not None:
            age_hours = max(0.0, (now_utc - pub).total_seconds() / 3600.0)
            rolling_s += decay_score(level_to_s(it.item_sentiment), age_hours, half_life_days)
        source_breakdown[it.source] = source_breakdown.get(it.source, 0) + 1
        if pub >= day24_start:
            mention_volume_24h += 1
        age_days = (now_utc - pub).total_seconds() / 86400.0
        oldest_age_days = max(oldest_age_days, age_days)

    n_items_30d = len(items)
    # Divide by the ACTUAL span of stored data in the window (ceil of the oldest item's age in
    # days), capped at the 30-day window and floored at 1 day — so a store holding only a few days
    # of data reports a TRUE daily baseline instead of an ~N/30 under-count that fakes a spike.
    span_days = max(1, min(window_days, math.ceil(oldest_age_days)))
    mention_volume_baseline = n_items_30d / span_days

    # Rank by decay-weighted engagement: engagement faded by the SAME half-life as sentiment, so a
    # stale high-engagement item (catalyst already priced in) loses to a fresh medium one.
    def _decayed_engagement(it: ContentItem) -> float:
        age_hours = max(0.0, (now_utc - _as_utc(it.published_ts)).total_seconds() / 3600.0)
        return decay_score(_engagement_weight(it), age_hours, half_life_days)

    top = sorted(
        items,
        key=lambda it: (_decayed_engagement(it), _as_utc(it.published_ts)),
        reverse=True,
    )
    top_item_ids = [it.id for it in top[:5]]

    digest = {
        "coin": coin.upper(),
        "updated_ts": now_utc.isoformat(),
        "rolling_s": rolling_s,
        "n_items_30d": n_items_30d,
        "mention_volume_24h": mention_volume_24h,
        "mention_volume_baseline": mention_volume_baseline,
        "top_item_ids": top_item_ids,
        "source_breakdown": source_breakdown,
        "one_line": "",
    }
    _atomic_write_text(_digest_path(content_dir, coin), json.dumps(digest, indent=2, default=str))
    return digest


# --------------------------------------------------------------------------- #
# purge                                                                        #
# --------------------------------------------------------------------------- #


def _days(n: float):
    from datetime import timedelta

    return timedelta(days=n)


def purge(content_dir, now: datetime, retain_days: int = 30) -> dict:
    """Evict everything older than the retention window.

    Day-GRANULAR eviction — both deletions share the SAME boundary so they can never disagree.
    A whole ``items-YYYY-MM-DD.jsonl`` day file is deleted iff its day is strictly before the
    cutoff DAY (``(now - retain_days).date()``), and each ``by_coin/<COIN>.jsonl`` index drops
    exactly the pointers whose published-day is strictly before that same cutoff day. Trimming by
    day-key (not by the cutoff TIME) is what keeps the invariant "purge never drops an in-window
    pointer": an item on the cutoff day itself — whose day file is KEPT — keeps its pointer too, so
    :func:`get_item` and :func:`index_for_coin` always agree (no orphaned body). Index rewrites are
    atomic; an index left empty is removed. Returns ``{files_deleted, pointers_dropped}``.
    """
    cutoff_day = (_as_utc(now) - _days(retain_days)).date()
    files_deleted = 0
    pointers_dropped = 0

    items_dir = _items_dir(content_dir)
    if items_dir.exists():
        for path in items_dir.glob("items-*.jsonl"):
            day_str = path.stem.removeprefix("items-")
            try:
                day = datetime.fromisoformat(day_str).date()
            except ValueError:
                continue  # not a recognisable day file — leave it alone
            if day < cutoff_day:
                path.unlink()
                files_deleted += 1

    index_dir = _index_dir(content_dir)
    if index_dir.exists():
        for path in index_dir.glob("*.jsonl"):
            pointers = _read_jsonl(path)
            kept: list[dict] = []
            for ptr in pointers:
                pub = ptr.get("published_ts")
                keep = False
                if pub:
                    try:
                        # day-key comparison, matching the day-file deletion exactly.
                        keep = _as_utc(datetime.fromisoformat(pub)).date() >= cutoff_day
                    except ValueError:
                        keep = False
                if keep:
                    kept.append(ptr)
                else:
                    pointers_dropped += 1
            if not kept:
                path.unlink()
            elif len(kept) != len(pointers):
                _atomic_write_text(path, "".join(json.dumps(p, default=str) + "\n" for p in kept))

    return {"files_deleted": files_deleted, "pointers_dropped": pointers_dropped}
