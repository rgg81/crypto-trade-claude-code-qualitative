"""Forums adapter — Bitcointalk RSS / generic crypto-forum RSS feeds.

Bitcointalk and most forum software (SMF, phpBB, Discourse) expose board/topic RSS. We treat each
configured feed uniformly via the shared RSS parser; a feed that 403s or 404s is skipped. Fully
fail-soft."""
from __future__ import annotations

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_bytes
from futures_fund.sources._rss import parse_feed
from futures_fund.sources.base import SourceAdapter, cfg_get

DEFAULT_FEEDS: list[str] = [
    # Bitcointalk "Bitcoin Discussion" board RSS (SMF .rss action).
    "https://bitcointalk.org/index.php?action=.xml;type=rss;board=1.0",
    # Bitcointalk "Altcoin Discussion" board RSS.
    "https://bitcointalk.org/index.php?action=.xml;type=rss;board=67.0",
]


class ForumsAdapter(SourceAdapter):
    """Crypto forum RSS feeds (Bitcointalk + generic).

    Forums reads its OWN upstreams — it must NOT mirror the shared NEWS feeds. The per-source
    config block the crawler hands every adapter carries a generic ``feeds`` key that is the news
    RSS list (consumed by the ``rss`` adapter); reading that here would make forums a duplicate
    news source. So forums reads a forums-specific ``forums_feeds`` key (or a nested
    ``forums.feeds`` block) and DEFAULTs to the Bitcointalk board feeds in :data:`DEFAULT_FEEDS` —
    never to the bare ``feeds`` news list.

    Config keys (optional): ``forums_feeds`` (list of forum RSS URLs, default :data:`DEFAULT_FEEDS`;
    a nested ``forums.feeds`` block is also honoured), ``per_feed`` (cap per feed, default 20).
    """

    name = "forums"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        feeds = self._feeds(cfg)
        per_feed = int(cfg_get(cfg, "per_feed", 20) or 20)
        seen: set[str] = set()
        out: list[ContentItem] = []
        for url in feeds:
            try:
                content = get_bytes(client, url, timeout=self.timeout_s)
                if not content:
                    continue
                host = url.split("//")[-1].split("/")[0]
                for row in parse_feed(content)[:per_feed]:
                    item = self._make_item(
                        feed=url,
                        url=row["url"],
                        title=row["title"],
                        body=row["body"],
                        author=row["author"],
                        published=row["published"],
                        universe=universe,
                        engagement={"forum": host},
                    )
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    out.append(item)
            except Exception:
                continue
        return out

    def _feeds(self, cfg) -> list[str]:
        """Resolve the forums-specific feed list, defaulting to Bitcointalk (:data:`DEFAULT_FEEDS`).

        Reads, in order: a ``forums_feeds`` key, then a nested ``forums.feeds`` block. The generic
        ``feeds`` key (the shared NEWS RSS list) is intentionally NOT consulted — forums must not
        mirror the news feeds. Any odd/empty shape falls back to the Bitcointalk defaults."""
        feeds = cfg_get(cfg, "forums_feeds", None)
        if not feeds:
            nested = cfg_get(cfg, "forums", None)
            feeds = cfg_get(nested, "feeds", None) if nested is not None else None
        if isinstance(feeds, (list, tuple)) and feeds:
            return [str(u) for u in feeds]
        return DEFAULT_FEEDS
