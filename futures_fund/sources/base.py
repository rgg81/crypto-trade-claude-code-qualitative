"""Pluggable source-adapter base — the common contract every content source implements.

A :class:`SourceAdapter` knows how to pull raw content from ONE upstream (an RSS feed, reddit,
Nitter, StockTwits, …) and normalise it into :class:`~futures_fund.content_store.ContentItem`s the
30-day content store can ingest. The cycle holds a registry of adapters (see
:mod:`futures_fund.sources`) and calls :meth:`SourceAdapter.fetch` on each enabled one.

Two hard rules every adapter MUST honour (mirroring base ``futures_fund/vendors.py``):

* **Fail-soft** — every network + parse path is wrapped; a dead/blocked/garbage upstream yields
  ``[]`` (or skips the bad item) and NEVER raises into the fetch loop.
* **Injected time** — ``fetched_ts`` comes from an injected ``now`` callable (defaulting to
  ``datetime.now(UTC)``), so the desk is deterministic and testable offline.

Instrument tagging is delegated to :func:`futures_fund.vendors.tag_instruments` (extended via a
local alias map for the full universe), so all sources tag coins identically.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime

from futures_fund import vendors
from futures_fund.content_store import ContentItem, make_id
from futures_fund.sentiment_decay import _parse_published

# Extra ticker<->name aliases beyond the small base map, covering more of the universe. The base
# vendors._ALIASES only carries a handful; we merge this in for tagging so e.g. "matic"/"polygon"
# resolve. Adding a coin = one entry here (the base map is read-only / shared).
_EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "BTC": ("btc", "bitcoin", "xbt"),
    "ETH": ("eth", "ethereum", "ether"),
    "SOL": ("sol", "solana"),
    "BNB": ("bnb", "binance coin"),
    "XRP": ("xrp", "ripple"),
    "DOGE": ("doge", "dogecoin"),
    "ADA": ("ada", "cardano"),
    "AVAX": ("avax", "avalanche"),
    "MATIC": ("matic", "polygon"),
    "DOT": ("dot", "polkadot"),
    "LINK": ("link", "chainlink"),
    "LTC": ("ltc", "litecoin"),
    "TRX": ("trx", "tron"),
    "SHIB": ("shib", "shiba", "shiba inu"),
    "ARB": ("arb", "arbitrum"),
    "OP": ("op", "optimism"),
    "ATOM": ("atom", "cosmos"),
    "NEAR": ("near",),
    "APT": ("apt", "aptos"),
    "SUI": ("sui",),
    "INJ": ("inj", "injective"),
}


def _ensure_aliases() -> None:
    """Merge the local extended alias map into vendors._ALIASES (idempotent).

    We extend the SHARED base map's known coins with extra keywords so every source tags coins
    consistently. We only ADD keywords / coins — never drop the originals."""
    for coin, kws in _EXTRA_ALIASES.items():
        existing = vendors._ALIASES.get(coin, ())
        merged = tuple(dict.fromkeys((*existing, *kws)))  # de-dupe, keep order
        vendors._ALIASES[coin] = merged


_ensure_aliases()


def _now_utc() -> datetime:
    return datetime.now(UTC)


def cfg_get(cfg, key: str, default=None):
    """Read ``key`` from a per-source config that may be a dict OR a pydantic/attr object.

    Adapters take config defensively — the registry may hand them a plain dict (from YAML), a
    nested dict under their name, or a settings object. Missing -> ``default``."""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


class SourceAdapter(ABC):
    """One pluggable content source.

    Subclasses set :attr:`name` and implement :meth:`fetch`. ``now`` is an injected clock used for
    ``fetched_ts`` (defaults to wall-clock UTC) so tests can pin time.
    """

    #: stable registry key (also the ContentItem.source value)
    name: str = "base"
    #: whether the adapter is eligible to run (the registry filter can still gate by config)
    enabled: bool = True
    #: per-request network timeout in seconds
    timeout_s: float = 8.0

    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or _now_utc

    # -- subclass contract ---------------------------------------------------

    @abstractmethod
    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        """Pull + normalise this source's recent content into ContentItems.

        MUST be fully fail-soft: wrap all network + parse work in try/except and return ``[]`` (or
        skip the offending item) rather than raise. ``client`` is an httpx-like client; ``cfg`` is
        the per-source config (a dict / pydantic model — adapters read what they need); ``universe``
        is the list of tradable symbols used for coin tagging.
        """
        raise NotImplementedError

    # -- shared helpers ------------------------------------------------------

    def _make_item(
        self,
        *,
        feed: str,
        url: str,
        title: str,
        universe: list[str],
        body: str = "",
        author: str = "",
        published: object = None,
        engagement: dict | None = None,
        tag_text: str | None = None,
    ) -> ContentItem:
        """Build a normalised ContentItem.

        ``fetched_ts`` is set from the injected clock; ``published_ts`` is parsed leniently from
        ``published`` (RFC-822 / ISO-8601), falling back to the fetched time when absent/garbage.
        Coins are tagged via :func:`vendors.tag_instruments` over ``tag_text`` (default: title +
        body), so a coin named only in the body is still tagged. ``id`` is the content-store stable
        sha1 over (source, url, title).
        """
        now = self._now()
        published_ts = _parse_published(published) or now
        text = tag_text if tag_text is not None else f"{title} {body}"
        coins = vendors.tag_instruments(text, universe)
        return ContentItem(
            id=make_id(self.name, url, title),
            source=self.name,
            feed=feed,
            url=url or "",
            title=title,
            body=body or "",
            author=author or "",
            published_ts=published_ts,
            fetched_ts=now,
            coins=coins,
            engagement=engagement or {},
        )
