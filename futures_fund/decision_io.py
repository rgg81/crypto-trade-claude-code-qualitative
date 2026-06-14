"""Assemble the per-coin evidence packet for the 4h decision loop.

This is the READ side of the qualitative desk: for each coin under consideration the decider needs
one self-contained packet — the coin's rolling decayed-sentiment :func:`digest
<futures_fund.content_store.update_digest>`, the in-window recent CONTENT items (headline + summary
+ analyst sentiment, point-in-time), a source breakdown, and a *non-directional* price card.

The price card (mark + ATR) is RISK PLUMBING ONLY. It exists so the decider/sizer can place a stop
and size a position; it is NOT a trading signal and must never be read as one — the directional view
comes from the qualitative evidence (digest + items), not from the price. The card carries an
explicit note saying so.

Price/ATR is fetched via an INJECTED ``price_fn(coin) -> (mark, atr)`` so this module — and its
tests — run fully offline. In production a thin wrapper over ``futures_fund.exchange`` /
``futures_fund.market_data`` supplies that callable (see :func:`make_price_fn`). Any failure of the
price function is FAIL-SOFT: the packet is still assembled, the price card carries a note explaining
the price was unavailable, and the decision loop is never crashed by a flaky market-data fetch.

Time is always INJECTED (`now`) — there is no global clock — so assembly is deterministic.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, Field

from futures_fund.content_store import _digest_path, get_item, index_for_coin

# The exact, load-bearing disclaimer stamped on every price card. The price card is RISK PLUMBING:
# it feeds stop placement and sizing ONLY — it is never a directional/trading signal.
PRICE_CARD_NOTE = (
    "NON-DIRECTIONAL: for stop placement & sizing ONLY — not a trading signal"
)

# Stamped when the injected price_fn is absent or fails — the packet is still usable, the
# directional evidence is unaffected, only the (non-directional) risk plumbing is missing.
PRICE_CARD_UNAVAILABLE_NOTE = (
    "PRICE UNAVAILABLE: mark/ATR could not be fetched — size/stop plumbing degraded, "
    "qualitative evidence unaffected"
)

# READ-LAYER recency cap. `recent_items` is newest-first and in-window (96h), but the window can
# still hold an unbounded pile of items for a busy coin — feeding all of them to the experts/decider
# buries the freshest read under stale bulk. Truncate to the newest N per coin so the read layer is
# recency-aware (NOTE: this is the READ layer only — the digest/decay math over the full window is
# untouched). 40 ≈ a healthy 96h tape without drowning the reader.
RECENT_ITEMS_CAP = 40

# Age-bucket boundaries (hours) for the per-row freshness label, and the freshness-fraction cutoff.
_FRESH_MAX_HOURS = 24.0
_RECENT_MAX_HOURS = 48.0


def _as_utc(dt: datetime) -> datetime:
    """Naive datetime is assumed UTC; aware is normalised to UTC."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class EvidencePacket(BaseModel):
    """Everything the decider needs about ONE coin at decision time, in one object.

    `digest` is the coin's rolling decayed-sentiment digest (or {} if none has been computed yet).
    `recent_items` are the in-store, in-window content items (newest first) reduced to the fields a
    reader needs. `price_card` is RISK PLUMBING (mark/ATR + a non-directional note) — never a
    signal; it is None when no price function was supplied AND no fallback note could be produced.
    `source_breakdown` counts the recent items by source.
    """

    coin: str
    as_of_ts: datetime
    digest: dict = Field(default_factory=dict)
    recent_items: list[dict] = Field(default_factory=list)
    price_card: dict | None = None
    source_breakdown: dict = Field(default_factory=dict)
    # Fraction (0.0–1.0) of the carried recent_items that are <=24h old — a one-glance signal of how
    # fresh the evidence backing this coin is. 0.0 when there are no recent_items.
    freshness: float = 0.0


def read_digest(content_dir, coin: str) -> dict:
    """Read the coin's persisted rolling-sentiment digest, or {} if none exists / is unreadable.

    Fail-soft: a missing or torn digest file degrades to an empty dict rather than crashing the
    decision loop."""
    path: Path = _digest_path(content_dir, coin)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _build_price_card(coin: str, price_fn) -> dict | None:
    """Fetch (mark, atr) via the injected `price_fn` and wrap it as a NON-DIRECTIONAL card.

    Fail-soft on every path: no price_fn, a price_fn that raises, or a malformed/non-numeric return
    all degrade to a card carrying the UNAVAILABLE note (mark/atr None) — never a raise into the
    decision loop. The directional evidence (digest + items) is independent of this card."""
    if price_fn is None:
        return {"mark": None, "atr": None, "note": PRICE_CARD_UNAVAILABLE_NOTE}
    try:
        mark, atr = price_fn(coin)
        mark_f = float(mark)
        atr_f = float(atr)
    except Exception:  # noqa: BLE001 — fail-soft: a flaky price fetch must never crash assembly
        return {"mark": None, "atr": None, "note": PRICE_CARD_UNAVAILABLE_NOTE}
    return {"mark": mark_f, "atr": atr_f, "note": PRICE_CARD_NOTE}


def _age_hours(published_ts: datetime, as_of: datetime) -> float:
    """Age of an item in hours vs the packet `as_of` timestamp, floored at 0.0.

    A (slightly) future-dated item — clock skew at the source — reads as age 0.0 rather than a
    negative age."""
    return max(0.0, (as_of - _as_utc(published_ts)).total_seconds() / 3600.0)


def _age_bucket(age_hours: float) -> str:
    """Coarse freshness label for a row: ``fresh<=24h`` / ``recent<=48h`` / ``stale>48h``."""
    if age_hours <= _FRESH_MAX_HOURS:
        return "fresh<=24h"
    if age_hours <= _RECENT_MAX_HOURS:
        return "recent<=48h"
    return "stale>48h"


def _recent_items(content_dir, coin: str, since: datetime, as_of: datetime) -> list[dict]:
    """In-store, in-window items for `coin` (published_ts >= since), newest first, reduced & capped.

    Filtering is done cheaply over the pointer index (`index_for_coin`); only the items that pass
    are hydrated via `get_item`. A pointer whose item is missing from the store is skipped — the
    packet only ever carries items that are BOTH in-store AND in-window.

    The read layer is recency-aware: each row carries a derived `age_hours` (vs the packet `as_of`)
    and an `age_bucket` label, and the newest-first list is TRUNCATED to the newest
    :data:`RECENT_ITEMS_CAP` items so a busy coin's 96h tape never buries the freshest read under
    stale bulk. (This is the READ layer only — the digest/decay math over the full window is
    untouched.)"""
    rows: list[dict] = []
    for ptr in index_for_coin(content_dir, coin, since):
        item = get_item(content_dir, ptr.get("item_id"))
        if item is None:
            continue  # in-window pointer but the item is not in the store — skip
        published = _as_utc(item.published_ts)
        age = _age_hours(published, as_of)
        rows.append(
            {
                "item_id": item.id,
                "title": item.title,
                "summary": item.summary,
                "source": item.source,
                "published_ts": published.isoformat(),
                "item_sentiment": item.item_sentiment,
                "age_hours": age,
                "age_bucket": _age_bucket(age),
            }
        )
    rows.sort(key=lambda r: r["published_ts"], reverse=True)
    return rows[:RECENT_ITEMS_CAP]  # newest-first, capped — keep only the freshest tape


def assemble_evidence(
    content_dir,
    coins: list[str],
    now: datetime,
    window_hours: float = 96.0,
    price_fn=None,
) -> dict[str, EvidencePacket]:
    """Build one :class:`EvidencePacket` per coin for the 4h decision loop.

    For each coin: read its rolling-sentiment `digest`, gather the in-store items published within
    the trailing `window_hours` (default 96h ≈ 4 days), summarise them into `source_breakdown`, and
    attach a NON-DIRECTIONAL price card (mark/ATR for stop+size plumbing only) via the injected
    `price_fn`. `now` is injected so assembly is deterministic and offline; `price_fn(coin)` is
    injected so no live market-data call is made here.

    Returns ``{COIN: EvidencePacket}`` keyed by the upper-cased coin symbol. The whole thing is
    fail-soft: a missing digest -> {}, a coin with no items -> empty recent_items, and a price fetch
    that fails -> a price card with the unavailable note. Nothing here raises into the loop.
    """
    now_utc = _as_utc(now)
    since = now_utc - timedelta(hours=window_hours)
    packets: dict[str, EvidencePacket] = {}
    for raw_coin in coins:
        coin = raw_coin.upper()
        items = _recent_items(content_dir, coin, since, now_utc)
        source_breakdown: dict[str, int] = {}
        for row in items:
            src = row["source"]
            source_breakdown[src] = source_breakdown.get(src, 0) + 1
        # fraction of carried items that are <=24h old (0.0 when there are none) — a one-glance
        # signal of how fresh the evidence is. Computed over the (capped) carried rows.
        n_fresh = sum(1 for row in items if row["age_hours"] <= _FRESH_MAX_HOURS)
        freshness = n_fresh / len(items) if items else 0.0
        packets[coin] = EvidencePacket(
            coin=coin,
            as_of_ts=now_utc,
            digest=read_digest(content_dir, coin),
            recent_items=items,
            price_card=_build_price_card(coin, price_fn),
            source_breakdown=source_breakdown,
            freshness=freshness,
        )
    return packets


def make_price_fn(exchange, symbol_for_coin, atr_for_coin):
    """Build a production `price_fn(coin) -> (mark, atr)` over the live exchange/market_data stack.

    `exchange` is a :class:`futures_fund.exchange.FuturesExchange`; `symbol_for_coin(coin)` maps a
    coin symbol (e.g. ``BTC``) to its unified ccxt symbol (e.g. ``BTC/USDT:USDT``); `atr_for_coin`
    supplies the ATR for that coin (typically computed from `exchange.ohlcv`). The price card it
    feeds is RISK PLUMBING ONLY (stop placement + sizing) — never a directional signal. Kept tiny
    and dependency-injected so it is trivially swapped for a fake in tests.
    """

    def price_fn(coin: str) -> tuple[float, float]:
        symbol = symbol_for_coin(coin)
        mark = exchange.mark_price(symbol)
        atr = atr_for_coin(coin)
        return float(mark), float(atr)

    return price_fn
