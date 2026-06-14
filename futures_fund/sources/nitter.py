"""Nitter adapter — keyless Twitter/X reads via Nitter mirror RSS endpoints.

Nitter mirrors are flaky (rate-limited / down), so we rotate a config list of base URLs: for each
handle (``/<handle>/rss``) and search term (``/search/rss?q=...``) we try mirror 1, and on ANY
failure advance to the next mirror. All mirrors dead for a target -> that target yields nothing.
Fully fail-soft."""
from __future__ import annotations

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_bytes
from futures_fund.sources._rss import parse_feed
from futures_fund.sources.base import SourceAdapter, cfg_get

DEFAULT_MIRRORS: list[str] = [
    "https://nitter.net",
    "https://nitter.poast.org",
    "https://nitter.privacydev.net",
]


class NitterAdapter(SourceAdapter):
    """Twitter/X via Nitter mirror RSS.

    Config keys (optional): ``mirrors`` (list of base URLs, default :data:`DEFAULT_MIRRORS`),
    ``handles`` (list of @-less handles), ``searches`` (list of query strings),
    ``per_target`` (cap per handle/search, default 20).
    """

    name = "nitter"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        mirrors = cfg_get(cfg, "mirrors") or DEFAULT_MIRRORS
        handles = cfg_get(cfg, "handles") or []
        searches = cfg_get(cfg, "searches") or []
        per_target = int(cfg_get(cfg, "per_target", 20) or 20)
        if not mirrors:
            return []
        seen: set[str] = set()
        out: list[ContentItem] = []
        for handle in handles:
            out.extend(
                self._collect(
                    client, mirrors, f"/{str(handle).lstrip('@')}/rss",
                    feed=f"nitter:@{str(handle).lstrip('@')}", author=str(handle).lstrip("@"),
                    universe=universe, per_target=per_target, seen=seen,
                )
            )
        for q in searches:
            out.extend(
                self._collect(
                    client, mirrors, "/search/rss", params={"f": "tweets", "q": str(q)},
                    feed=f"nitter:search:{q}", author="", universe=universe,
                    per_target=per_target, seen=seen,
                )
            )
        return out

    def _collect(
        self, client, mirrors, path, *, feed, author, universe, per_target, seen,
        params: dict | None = None,
    ) -> list[ContentItem]:
        """Try each mirror in turn for one target; first mirror that yields rows wins."""
        for base in mirrors:
            try:
                content = get_bytes(
                    client, str(base).rstrip("/") + path, timeout=self.timeout_s, params=params
                )
                if not content:
                    continue
                rows = parse_feed(content)
                if not rows:
                    continue
                out: list[ContentItem] = []
                for row in rows[:per_target]:
                    item = self._make_item(
                        feed=feed,
                        url=row["url"],
                        title=row["title"],
                        body=row["body"],
                        author=row["author"] or author,
                        published=row["published"],
                        universe=universe,
                        engagement={"mirror": str(base)},
                    )
                    if item.id in seen:
                        continue
                    seen.add(item.id)
                    out.append(item)
                return out  # this mirror served the target — stop rotating
            except Exception:
                continue  # advance to the next mirror
        return []
