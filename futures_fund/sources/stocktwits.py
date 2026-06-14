"""StockTwits adapter — keyless symbol-stream reads.

GET ``https://api.stocktwits.com/api/2/streams/symbol/<TICKER>.X.json`` (the ``.X`` crypto suffix,
e.g. ``BTC.X``). Each message's body becomes the item; the per-message
``entities.sentiment.basic`` (``Bullish``/``Bearish``) is folded into engagement so the desk can
read crowd lean.

QUALITY GATE — StockTwits dominates the corpus but is mostly low-signal: bare cashtag spam ("$BTC
🚀") and multi-ticker pump posts that bait-list a dozen unrelated coins. Two filters keep the
crowd read honest, both fully fail-soft:

* A message whose body — stripped of cashtags, emoji and whitespace — is shorter than
  ``min_body_chars`` is DROPPED, UNLESS it carries an explicit StockTwits Bullish/Bearish label
  (an explicit lean is signal even in a one-liner).
* Tagging is restricted to the SUBSTANTIVE ticker. A post that fans out many cashtags is a pump /
  bait post; we tag only the streamed ticker (the symbol whose stream we queried — the subject of
  this message) rather than every incidental cashtag, so bait tickers don't pollute their digests.
"""
from __future__ import annotations

import re

from futures_fund.content_store import ContentItem
from futures_fund.sources._http import get_json
from futures_fund.sources.base import SourceAdapter, cfg_get
from futures_fund.vendors import _base

API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.X.json"

# A "$BTC"-style cashtag (the ticker may carry the ".X" crypto suffix). Used both to COUNT how many
# distinct tickers a post lists (bait detection) and to STRIP them when measuring real body length.
_CASHTAG_RE = re.compile(r"\$[A-Za-z][A-Za-z0-9.]*")
# Anything that is not a letter or digit — emoji, punctuation, whitespace — collapses to nothing
# when measuring the substantive character count of a body.
_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")

# A post listing more than this many DISTINCT cashtags is treated as a pump / bait post: tagging is
# restricted to the streamed (substantive) ticker so the bait tickers don't fan out.
_MAX_CASHTAGS_BEFORE_BAIT = 3


class StockTwitsAdapter(SourceAdapter):
    """Keyless StockTwits crypto symbol streams.

    Config keys (optional): ``tickers`` (explicit ticker list, e.g. ``["BTC","ETH"]``); when
    absent the adapter derives tickers from the trading ``universe``. ``per_symbol`` caps messages
    per ticker (default 20). ``min_body_chars`` (default 25) is the minimum substantive body length
    — a shorter, label-less message is dropped as noise.
    """

    name = "stocktwits"

    def fetch(self, client, cfg, universe: list[str]) -> list[ContentItem]:
        tickers = cfg_get(cfg, "tickers")
        if not tickers:
            # de-dupe while preserving order
            tickers = list(dict.fromkeys(_base(s) for s in universe))
        per_symbol = int(cfg_get(cfg, "per_symbol", 20) or 20)
        min_body_chars = int(cfg_get(cfg, "min_body_chars", 25) or 25)
        seen: set[str] = set()
        out: list[ContentItem] = []
        for ticker in tickers:
            t = str(ticker).upper()
            payload = get_json(client, API.format(ticker=t), timeout=self.timeout_s)
            for item in self._from_payload(payload, t, universe, per_symbol, min_body_chars):
                if item.id in seen:
                    continue
                seen.add(item.id)
                out.append(item)
        return out

    def _from_payload(
        self, payload, ticker, universe, per_symbol, min_body_chars
    ) -> list[ContentItem]:
        try:
            messages = (payload or {}).get("messages", [])
        except (AttributeError, TypeError):
            return []
        out: list[ContentItem] = []
        for msg in messages[:per_symbol]:
            try:
                if not isinstance(msg, dict):
                    continue
                body = (msg.get("body") or "").strip()
                if not body:
                    continue
                sentiment = _sentiment(msg)  # "Bullish" | "Bearish" | None
                # NOISE GATE: a sub-min-length body is kept ONLY if it carries an explicit lean.
                if sentiment is None and _substantive_len(body) < min_body_chars:
                    continue
                mid = msg.get("id")
                user = msg.get("user") or {}
                author = user.get("username") if isinstance(user, dict) else ""
                likes = msg.get("likes") or {}
                eng = {
                    "ticker": ticker,
                    "sentiment": sentiment,
                    "likes": int(likes.get("total") or 0) if isinstance(likes, dict) else 0,
                }
                # title = first line of the body (StockTwits posts have no title)
                title = body.splitlines()[0][:140] if body else f"${ticker}"
                out.append(
                    self._make_item(
                        feed=f"stocktwits:{ticker}.X",
                        url=f"https://stocktwits.com/message/{mid}" if mid else "",
                        title=title,
                        body=body,
                        author=author or "",
                        published=msg.get("created_at"),
                        universe=universe,
                        engagement=eng,
                        tag_text=_tag_text(ticker, title, body),
                    )
                )
            except Exception:
                continue
        return out


def _substantive_len(body: str) -> int:
    """Length of ``body`` after dropping cashtags, emoji and whitespace — the real prose length.

    "$BTC 🚀" -> 0; "$BTC to the moon" -> len("tothemoon")=9. Used by the noise gate so bare
    cashtag/emoji spam is measured as the empty signal it is."""
    stripped = _CASHTAG_RE.sub(" ", body)
    return len(_NON_ALNUM_RE.sub("", stripped))


def _tag_text(ticker: str, title: str, body: str) -> str:
    """Build the tagging text, restricted to the SUBSTANTIVE ticker on bait/pump posts.

    A post that lists more than :data:`_MAX_CASHTAGS_BEFORE_BAIT` distinct cashtags is a
    multi-ticker pump post; tagging its full body would fan the item out to every bait ticker. In
    that case we tag ONLY the streamed ``ticker`` (the subject of this symbol stream). Otherwise we
    tag on ticker + title + body so a coin named in normal prose still tags."""
    cashtags = {m.group(0).lstrip("$").split(".")[0].upper() for m in _CASHTAG_RE.finditer(body)}
    if len(cashtags) > _MAX_CASHTAGS_BEFORE_BAIT:
        return ticker
    return f"{ticker} {title} {body}"


def _sentiment(msg: dict):
    ent = msg.get("entities")
    if isinstance(ent, dict):
        sent = ent.get("sentiment")
        if isinstance(sent, dict):
            return sent.get("basic")
    return None
