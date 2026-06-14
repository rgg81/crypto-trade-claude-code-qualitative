"""YouTube adapter — keyless channel-uploads RSS + transcript scrape.

Per channel we fetch the uploads feed (``feeds/videos.xml?channel_id=...``); for each video we try
the keyless ``timedtext`` transcript endpoint and use a transcript excerpt as the body. If the
transcript is missing/empty we fall back to the video title + RSS description. Fully fail-soft."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_bytes
from futures_fund.sources._rss import clean_html, parse_feed
from futures_fund.sources.base import SourceAdapter, cfg_get

UPLOADS_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
TIMEDTEXT = "https://www.youtube.com/api/timedtext"

_VID_RE = re.compile(r"(?:v=|/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{11})")


class YouTubeAdapter(SourceAdapter):
    """Keyless YouTube channel uploads + transcripts.

    Config keys (optional): ``channel_ids`` (list of UC… channel ids), ``per_channel`` (cap videos
    per channel, default 5), ``lang`` (transcript language, default ``en``), ``transcript_chars``
    (excerpt length, default 1200).
    """

    name = "youtube"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        channel_ids = cfg_get(cfg, "channel_ids") or []
        per_channel = int(cfg_get(cfg, "per_channel", 5) or 5)
        lang = cfg_get(cfg, "lang", "en") or "en"
        excerpt = int(cfg_get(cfg, "transcript_chars", 1200) or 1200)
        seen: set[str] = set()
        out: list[ContentItem] = []
        for cid in channel_ids:
            content = get_bytes(client, UPLOADS_FEED.format(cid=cid), timeout=self.timeout_s)
            if not content:
                continue
            for row in parse_feed(content)[:per_channel]:
                try:
                    item = self._video_item(client, row, str(cid), universe, lang, excerpt)
                    if item is None or item.id in seen:
                        continue
                    seen.add(item.id)
                    out.append(item)
                except Exception:
                    continue
        return out

    def _video_item(self, client, row, cid, universe, lang, excerpt) -> ContentItem | None:
        title = row.get("title")
        if not title:
            return None
        url = row.get("url") or ""
        vid = _video_id(url)
        transcript = self._transcript(client, vid, lang, excerpt) if vid else ""
        # body: transcript excerpt, else fall back to title + RSS description
        body = transcript or clean_html(f"{title}. {row.get('body', '')}", excerpt)
        return self._make_item(
            feed=f"youtube:{cid}",
            url=url,
            title=title,
            body=body,
            author=row.get("author") or "",
            published=row.get("published"),
            universe=universe,
            engagement={"channel_id": cid, "has_transcript": bool(transcript)},
            tag_text=f"{title} {body}",
        )

    def _transcript(self, client, vid: str, lang: str, excerpt: int) -> str:
        """Fetch + parse the timedtext XML transcript -> plain-text excerpt. '' if absent."""
        content = get_bytes(
            client, TIMEDTEXT, timeout=self.timeout_s, params={"lang": lang, "v": vid}
        )
        if not content:
            return ""
        try:
            root = ET.fromstring(content)
        except (ET.ParseError, TypeError, ValueError):
            return ""
        parts = [t.text for t in root.findall(".//text") if t is not None and t.text]
        if not parts:
            return ""
        return clean_html(" ".join(parts), excerpt)


def _video_id(url: str) -> str:
    if not url:
        return ""
    m = _VID_RE.search(url)
    return m.group(1) if m else ""
