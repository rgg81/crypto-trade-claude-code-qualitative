"""Reddit adapter — forks base ``vendors.fetch_reddit`` / ``_posts_for_sub``.

Per subreddit it tries the richer ``/hot.json`` (carries upvote score) first, which reddit OFTEN
403s for keyless/datacenter reads, then falls back to the ``/.rss`` Atom feed (works keyless, no
score). Both blocked -> the sub is skipped. Fully fail-soft."""
from __future__ import annotations

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_bytes, get_json
from futures_fund.sources._rss import clean_html, parse_feed
from futures_fund.sources.base import SourceAdapter, cfg_get

DEFAULT_SUBREDDITS: list[str] = ["CryptoCurrency", "CryptoMarkets"]


class RedditAdapter(SourceAdapter):
    """Keyless reddit social scrape.

    Config keys (optional): ``subreddits`` (list, default :data:`DEFAULT_SUBREDDITS`),
    ``per_sub`` (cap per subreddit, default 40).
    """

    name = "reddit"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        subs = cfg_get(cfg, "subreddits") or cfg_get(cfg, "reddit_subreddits") or DEFAULT_SUBREDDITS
        per_sub = int(cfg_get(cfg, "per_sub", 40) or 40)
        seen: set[str] = set()
        out: list[ContentItem] = []
        for sub in subs:
            for item in self._posts_for_sub(client, sub, universe, per_sub):
                if item.id in seen:
                    continue
                seen.add(item.id)
                out.append(item)
        return out

    def _posts_for_sub(
        self, client, sub: str, universe: list[str], per_sub: int
    ) -> list[ContentItem]:
        # 1) richer JSON listing (upvote score) — often 403s keyless
        payload = get_json(
            client,
            f"https://www.reddit.com/r/{sub}/hot.json",
            timeout=self.timeout_s,
            params={"limit": per_sub},
        )
        items = self._from_json(payload, sub, universe)
        if items:
            return items[:per_sub]
        # 2) keyless RSS fallback (no score)
        content = get_bytes(client, f"https://www.reddit.com/r/{sub}/.rss", timeout=self.timeout_s)
        if not content:
            return []
        out: list[ContentItem] = []
        for row in parse_feed(content)[:per_sub]:
            try:
                out.append(
                    self._make_item(
                        feed=f"reddit:/r/{sub}",
                        url=row["url"],
                        title=row["title"],
                        body=row["body"],
                        author=row["author"],
                        published=row["published"],
                        universe=universe,
                        engagement={"subreddit": sub},
                    )
                )
            except Exception:
                continue
        return out

    def _from_json(self, payload, sub: str, universe: list[str]) -> list[ContentItem]:
        try:
            children = (payload or {}).get("data", {}).get("children", [])
        except (AttributeError, TypeError):
            return []
        out: list[ContentItem] = []
        for ch in children:
            try:
                d = ch.get("data", {}) if isinstance(ch, dict) else {}
                title = (d.get("title") or "").strip()
                if not title:
                    continue
                body = clean_html(d.get("selftext") or "")
                created = d.get("created_utc")
                out.append(
                    self._make_item(
                        feed=f"reddit:/r/{sub}",
                        url=("https://www.reddit.com" + d.get("permalink", ""))
                        if d.get("permalink")
                        else (d.get("url") or ""),
                        title=title,
                        body=body,
                        author=(d.get("author") or ""),
                        published=_epoch_to_iso(created),
                        universe=universe,
                        engagement={
                            "subreddit": sub,
                            "score": int(d.get("score") or 0),
                            "num_comments": int(d.get("num_comments") or 0),
                            "upvotes": int(d.get("ups") or 0),
                        },
                    )
                )
            except Exception:
                continue
        return out


def _epoch_to_iso(created) -> str:
    """reddit's created_utc is a unix epoch float -> ISO string for lenient parsing. '' on junk."""
    try:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(float(created), tz=UTC).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""
