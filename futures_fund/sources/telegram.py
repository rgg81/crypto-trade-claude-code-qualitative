"""Telegram adapter — keyless public-channel preview scrape.

GET ``https://t.me/s/<channel>`` returns the channel's public web preview HTML. Each message lives
in a ``<div class="tgme_widget_message_text">…</div>`` block; we extract those blocks, strip HTML
with the shared cleaner, and mint one item per message. Fully fail-soft."""
from __future__ import annotations

import re

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_bytes
from futures_fund.sources._rss import clean_html
from futures_fund.sources.base import SourceAdapter, cfg_get

# Each message in the t.me/s/ preview is a wrapper element carrying its OWN permalink in a
# data-post attribute. We split on that attribute so each chunk is one message, then read the
# text div (if any) WITHIN that chunk — this keeps every text block paired with its own message's
# permalink even when a non-text message (photo/poll) precedes text ones.
_POST_SPLIT_RE = re.compile(r'data-post="([^"]+)"', re.IGNORECASE)
# The message-text div block in the t.me/s/ preview HTML.
_MSG_RE = re.compile(
    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
    re.DOTALL | re.IGNORECASE,
)


class TelegramAdapter(SourceAdapter):
    """Keyless Telegram public channel previews.

    Config keys (optional): ``channels`` (list of public channel usernames), ``per_channel``
    (cap messages per channel, default 20).
    """

    name = "telegram"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        channels = cfg_get(cfg, "channels") or []
        per_channel = int(cfg_get(cfg, "per_channel", 20) or 20)
        seen: set[str] = set()
        out: list[ContentItem] = []
        for channel in channels:
            ch = str(channel).lstrip("@")
            content = get_bytes(client, f"https://t.me/s/{ch}", timeout=self.timeout_s)
            if not content:
                continue
            for item in self._from_html(content, ch, universe, per_channel):
                if item.id in seen:
                    continue
                seen.add(item.id)
                out.append(item)
        return out

    @staticmethod
    def _split_messages(text: str) -> list[tuple[str, str]]:
        """Split the preview HTML into ``(post_id, text_div_inner_html)`` per message, in order.

        Each message wrapper carries its own ``data-post`` permalink. We slice the document on those
        markers so every chunk is one message, then pull the FIRST text div within that chunk (a
        non-text message — photo/poll — contributes ``""`` and is dropped downstream). Pairing the
        text with its own enclosing message's permalink avoids the positional mis-attribution that a
        global zip of all permalinks against only the text-bearing blocks would introduce when a
        non-text message precedes text ones."""
        out: list[tuple[str, str]] = []
        matches = list(_POST_SPLIT_RE.finditer(text))
        for idx, m in enumerate(matches):
            post_id = m.group(1)
            start = m.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            chunk = text[start:end]
            block = _MSG_RE.search(chunk)
            raw = block.group(1) if block else ""
            out.append((post_id, raw))
        return out

    def _from_html(self, content: bytes, channel: str, universe, per_channel) -> list[ContentItem]:
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            return []
        try:
            messages = self._split_messages(text)
        except Exception:
            return []
        out: list[ContentItem] = []
        for post_id, raw in messages[:per_channel]:
            try:
                body = clean_html(raw)
                if not body:
                    continue
                title = body.splitlines()[0][:140] if body else f"@{channel}"
                out.append(
                    self._make_item(
                        feed=f"telegram:@{channel}",
                        url=f"https://t.me/{post_id}" if post_id else f"https://t.me/s/{channel}",
                        title=title,
                        body=body,
                        author=channel,
                        published=None,  # preview HTML has no machine ts -> fetched_ts
                        universe=universe,
                        engagement={"channel": channel},
                    )
                )
            except Exception:
                continue
        return out
