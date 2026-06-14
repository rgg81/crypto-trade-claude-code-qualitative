"""RSS news adapter — forks base ``vendors.fetch_news`` / ``parse_rss`` over a config list of
keyless crypto-news RSS feeds. Each feed degrades independently; a dead/blocked feed is skipped."""
from __future__ import annotations

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_bytes
from futures_fund.sources._rss import parse_feed
from futures_fund.sources.base import SourceAdapter, cfg_get

DEFAULT_FEEDS: list[str] = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.cryptoslate.com/feed/",
    "https://bitcoinmagazine.com/feed",
    "https://cryptopotato.com/feed/",
]


class RssAdapter(SourceAdapter):
    """Keyless crypto-news RSS feeds.

    Config keys (all optional): ``feeds`` (list of feed URLs, default :data:`DEFAULT_FEEDS`),
    ``per_source`` (cap per feed, default 10).
    """

    name = "rss"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        feeds = cfg_get(cfg, "feeds") or cfg_get(cfg, "news_rss_sources") or DEFAULT_FEEDS
        per_source = int(cfg_get(cfg, "per_source", 10) or 10)
        seen: set[str] = set()
        out: list[ContentItem] = []
        for url in feeds:
            try:
                content = get_bytes(client, url, timeout=self.timeout_s)
                if not content:
                    continue
                src = url.split("//")[-1].split("/")[0]
                for row in parse_feed(content)[:per_source]:
                    item = self._make_item(
                        feed=url,
                        url=row["url"],
                        title=row["title"],
                        body=row["body"],
                        author=row["author"],
                        published=row["published"],
                        universe=universe,
                        engagement={"feed_host": src},
                    )
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    out.append(item)
            except Exception:
                continue  # a dead/blocked feed must not break the cycle
        return out
