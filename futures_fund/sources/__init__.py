"""Pluggable source-adapter registry.

Each content source is a :class:`~futures_fund.sources.base.SourceAdapter`. They are registered in
:data:`SOURCE_ADAPTERS` (``{name: instance}``) so the cycle can iterate them uniformly. Adding a
new source = write the adapter + add ONE entry here.

:func:`enabled_adapters` filters the registry by a config ``sources`` block — a list of enabled
source names (e.g. ``["rss", "reddit", "youtube"]``). When the block is absent the adapter's own
:attr:`~SourceAdapter.enabled` default decides; an unknown name in the block is ignored (never
raises). Order in the returned list follows the config when given, else registry order.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from futures_fund.sources.base import SourceAdapter, cfg_get
from futures_fund.sources.forums import ForumsAdapter
from futures_fund.sources.nitter import NitterAdapter
from futures_fund.sources.reddit import RedditAdapter
from futures_fund.sources.rss import RssAdapter
from futures_fund.sources.stocktwits import StockTwitsAdapter
from futures_fund.sources.telegram import TelegramAdapter
from futures_fund.sources.youtube import YouTubeAdapter

# Adding a source = one entry. Instances share the default wall-clock; the cycle can rebuild them
# with an injected `now` via build_adapters() for deterministic replay.
SOURCE_ADAPTERS: dict[str, SourceAdapter] = {
    a.name: a
    for a in (
        RssAdapter(),
        RedditAdapter(),
        NitterAdapter(),
        StockTwitsAdapter(),
        TelegramAdapter(),
        YouTubeAdapter(),
        ForumsAdapter(),
    )
}

_ADAPTER_CLASSES = (
    RssAdapter,
    RedditAdapter,
    NitterAdapter,
    StockTwitsAdapter,
    TelegramAdapter,
    YouTubeAdapter,
    ForumsAdapter,
)


def build_adapters(now: Callable[[], datetime] | None = None) -> dict[str, SourceAdapter]:
    """Build a fresh registry with an injected clock (for deterministic offline replay)."""
    return {cls.name: cls(now=now) for cls in _ADAPTER_CLASSES}


def enabled_adapters(
    cfg, registry: dict[str, SourceAdapter] | None = None
) -> list[SourceAdapter]:
    """Return the enabled adapters, filtered by the config ``sources`` block.

    ``cfg`` may be a dict, a settings object, or the data-settings block — anything with a
    ``sources`` attribute/key (a list of enabled source names). When that block is absent we fall
    back to each adapter's :attr:`SourceAdapter.enabled` flag. Unknown names are ignored.

    FULLY FAIL-SOFT — this runs BEFORE the protected adapter pool in :func:`crawler.crawl_tick`, so
    a raise here would abort the WHOLE tick. A missing/None/non-list/garbage ``sources`` block (e.g.
    ``{"sources": 123}`` or a bare string) is therefore treated as "no usable filter" and falls back
    to the sane default set (every registered enabled adapter). Within a list, non-string / unknown
    entries are simply skipped. This never raises for any input.
    """
    reg = registry if registry is not None else SOURCE_ADAPTERS
    default = [a for a in reg.values() if a.enabled]

    try:
        names = cfg_get(cfg, "sources", None)
    except Exception:  # noqa: BLE001 — a hostile cfg object must not abort the tick
        return default

    # Only a genuine list/tuple of names is a usable filter. A scalar, dict, bool, bare string, or
    # any other non-list shape is malformed -> fall back to the default set rather than iterating
    # (a bare string would otherwise iterate per-character; a scalar would raise).
    if not isinstance(names, (list, tuple)) or not names:
        return default

    out: list[SourceAdapter] = []
    for name in names:
        if not isinstance(name, str):
            continue  # junk entry (None / int / dict / …) — skip, never raise
        a = reg.get(name)
        if a is not None and a.enabled:
            out.append(a)
    return out


__all__ = [
    "SOURCE_ADAPTERS",
    "SourceAdapter",
    "ForumsAdapter",
    "NitterAdapter",
    "RedditAdapter",
    "RssAdapter",
    "StockTwitsAdapter",
    "TelegramAdapter",
    "YouTubeAdapter",
    "build_adapters",
    "enabled_adapters",
]
