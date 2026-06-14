"""Offline tests for the pluggable source-adapter package (futures_fund/sources).

No live network: a FAKE httpx-like client routes URLs to canned fixtures. Every adapter is asserted
to (a) return well-formed ContentItems with coins tagged from the fixtures, and (b) return [] —
never raise — when the client raises or returns malformed bytes. Time is injected.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from futures_fund.content_store import ContentItem
from futures_fund.sources import (
    SOURCE_ADAPTERS,
    ForumsAdapter,
    NitterAdapter,
    RedditAdapter,
    RssAdapter,
    StockTwitsAdapter,
    TelegramAdapter,
    YouTubeAdapter,
    build_adapters,
    enabled_adapters,
)

FIXTURES = Path(__file__).parent / "fixtures" / "content"
NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
UNIVERSE = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT", "ADA/USDT:USDT"]


def _now() -> datetime:
    return NOW


def _fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --------------------------------------------------------------------------- #
# fake http client                                                            #
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class FakeClient:
    """Routes GET(url) to a fixture by substring match; unmatched -> empty 200."""

    def __init__(self, routes: dict[str, bytes]):
        self.routes = routes
        self.calls: list[str] = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(url)
        for needle, content in self.routes.items():
            if needle in url:
                return _Resp(content)
        return _Resp(b"")


class RaisingClient:
    def get(self, *a, **k):
        raise RuntimeError("network down")


class MalformedClient:
    """Returns non-XML / non-JSON garbage for everything."""

    def get(self, *a, **k):
        return _Resp(b"<<<not xml or json >>> \x00\xff garbage")


class _SizedResp:
    """A response whose body length and optional Content-Length header are controllable, for the
    _http size-cap guard. Mirrors the httpx-ish shape (content/json/headers/raise_for_status)."""

    def __init__(self, content: bytes, *, headers: dict | None = None):
        self.content = content
        self.headers = headers if headers is not None else {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.content.decode("utf-8"))


class _SizedClient:
    def __init__(self, resp: _SizedResp):
        self._resp = resp

    def get(self, url, headers=None, params=None, timeout=None):
        return self._resp


def test_http_get_bytes_caps_oversized_body() -> None:
    # HYGIENE: an untrusted public feed returning a body larger than the cap must be ABORTED
    # (return None) rather than buffered unbounded into memory.
    from futures_fund.sources import _http

    big = b"x" * (_http.MAX_RESPONSE_BYTES + 1)
    client = _SizedClient(_SizedResp(big))
    assert _http.get_bytes(client, "https://feed/x") is None
    # a body at or under the cap still returns normally.
    ok = b"y" * 10
    assert _http.get_bytes(_SizedClient(_SizedResp(ok)), "https://feed/x") == ok


def test_http_get_bytes_aborts_on_oversized_content_length_header() -> None:
    # if the server ADVERTISES an oversized Content-Length, abort BEFORE reading the body.
    from futures_fund.sources import _http

    resp = _SizedResp(b"small-actual-body",
                      headers={"Content-Length": str(_http.MAX_RESPONSE_BYTES + 1)})
    assert _http.get_bytes(_SizedClient(resp), "https://feed/x") is None


def test_http_get_bytes_respects_custom_cap() -> None:
    from futures_fund.sources import _http

    client = _SizedClient(_SizedResp(b"abcdef"))
    assert _http.get_bytes(client, "https://feed/x", max_bytes=3) is None
    assert _http.get_bytes(client, "https://feed/x", max_bytes=100) == b"abcdef"


def test_http_get_json_caps_oversized_body() -> None:
    from futures_fund.sources import _http

    big = b'{"k":"' + b"x" * (_http.MAX_RESPONSE_BYTES + 1) + b'"}'
    assert _http.get_json(_SizedClient(_SizedResp(big)), "https://feed/x") is None
    # a small JSON body still parses.
    assert _http.get_json(_SizedClient(_SizedResp(b'{"k":1}')), "https://feed/x") == {"k": 1}


def _coins(items: list[ContentItem]) -> set[str]:
    out: set[str] = set()
    for it in items:
        out.update(it.coins)
    return out


# --------------------------------------------------------------------------- #
# RSS                                                                          #
# --------------------------------------------------------------------------- #


def test_rss_adapter_returns_tagged_items() -> None:
    client = FakeClient({"coindesk": _fx("news.rss.xml")})
    adapter = RssAdapter(now=_now)
    items = adapter.fetch(
        client, {"feeds": ["https://www.coindesk.com/rss"]}, UNIVERSE
    )
    assert items, "expected news items"
    assert all(isinstance(i, ContentItem) for i in items)
    assert all(i.source == "rss" for i in items)
    assert all(i.fetched_ts == NOW for i in items)
    titles = {i.title for i in items}
    assert any("Bitcoin rips" in t for t in titles)
    coins = _coins(items)
    assert "BTC" in coins and "ETH" in coins and "SOL" in coins
    # body carried (HTML stripped)
    btc_item = next(i for i in items if "Bitcoin rips" in i.title)
    assert "<" not in btc_item.body and btc_item.body


def test_rss_adapter_failsoft_on_raise_and_garbage() -> None:
    adapter = RssAdapter(now=_now)
    assert adapter.fetch(RaisingClient(), {"feeds": ["x"]}, UNIVERSE) == []
    assert adapter.fetch(MalformedClient(), {"feeds": ["x"]}, UNIVERSE) == []


# --------------------------------------------------------------------------- #
# reddit                                                                       #
# --------------------------------------------------------------------------- #


def test_reddit_adapter_json_path() -> None:
    client = FakeClient({"hot.json": _fx("reddit_hot.json")})
    adapter = RedditAdapter(now=_now)
    items = adapter.fetch(client, {"subreddits": ["CryptoCurrency"]}, UNIVERSE)
    assert items
    assert all(i.source == "reddit" for i in items)
    # empty-title row skipped
    assert all(i.title for i in items)
    eth = next(i for i in items if "ETH" in i.coins)
    assert eth.engagement.get("score") == 1423
    assert "ETH" in _coins(items) and "BTC" in _coins(items)


def test_reddit_adapter_rss_fallback_when_json_blocked() -> None:
    # hot.json route absent -> JSON returns empty 200 -> falls back to .rss
    client = FakeClient({".rss": _fx("reddit.rss.xml")})
    adapter = RedditAdapter(now=_now)
    items = adapter.fetch(client, {"subreddits": ["CryptoCurrency"]}, UNIVERSE)
    assert items
    assert any("BTC" in i.coins for i in items)


def test_reddit_adapter_failsoft() -> None:
    adapter = RedditAdapter(now=_now)
    assert adapter.fetch(RaisingClient(), {"subreddits": ["X"]}, UNIVERSE) == []
    assert adapter.fetch(MalformedClient(), {"subreddits": ["X"]}, UNIVERSE) == []


# --------------------------------------------------------------------------- #
# nitter                                                                       #
# --------------------------------------------------------------------------- #


def test_nitter_adapter_rotates_to_working_mirror() -> None:
    # first mirror unmatched -> empty 200 -> rotate to the second which serves the rss fixture
    client = FakeClient({"nitter.poast.org": _fx("nitter.rss.xml")})
    adapter = NitterAdapter(now=_now)
    cfg = {
        "mirrors": ["https://nitter.net", "https://nitter.poast.org"],
        "handles": ["cryptoanalyst"],
    }
    items = adapter.fetch(client, cfg, UNIVERSE)
    assert items
    assert all(i.source == "nitter" for i in items)
    assert "BTC" in _coins(items) and "SOL" in _coins(items)
    assert any(i.engagement.get("mirror") == "https://nitter.poast.org" for i in items)


def test_nitter_no_targets_returns_empty() -> None:
    client = FakeClient({"nitter": _fx("nitter.rss.xml")})
    adapter = NitterAdapter(now=_now)
    assert adapter.fetch(client, {"mirrors": ["https://nitter.net"]}, UNIVERSE) == []


def test_nitter_adapter_failsoft() -> None:
    adapter = NitterAdapter(now=_now)
    cfg = {"mirrors": ["https://nitter.net"], "handles": ["x"]}
    assert adapter.fetch(RaisingClient(), cfg, UNIVERSE) == []
    assert adapter.fetch(MalformedClient(), cfg, UNIVERSE) == []


# --------------------------------------------------------------------------- #
# stocktwits                                                                   #
# --------------------------------------------------------------------------- #


def test_stocktwits_adapter_maps_sentiment() -> None:
    client = FakeClient({"streams/symbol/BTC.X": _fx("stocktwits.json")})
    adapter = StockTwitsAdapter(now=_now)
    items = adapter.fetch(client, {"tickers": ["BTC"]}, UNIVERSE)
    assert items
    assert all(i.source == "stocktwits" for i in items)
    # empty-body message skipped
    assert all(i.body for i in items)
    sentiments = {i.engagement.get("sentiment") for i in items}
    assert "Bullish" in sentiments and "Bearish" in sentiments
    assert "BTC" in _coins(items)


def test_stocktwits_derives_tickers_from_universe() -> None:
    client = FakeClient({"streams/symbol/BTC.X": _fx("stocktwits.json")})
    adapter = StockTwitsAdapter(now=_now)
    items = adapter.fetch(client, {}, UNIVERSE)  # no explicit tickers
    assert items and "BTC" in _coins(items)


def test_stocktwits_adapter_failsoft() -> None:
    adapter = StockTwitsAdapter(now=_now)
    assert adapter.fetch(RaisingClient(), {"tickers": ["BTC"]}, UNIVERSE) == []
    assert adapter.fetch(MalformedClient(), {"tickers": ["BTC"]}, UNIVERSE) == []


def test_stocktwits_noise_gate_drops_short_zero_sentiment_post() -> None:
    # The ADA stream carries a bare "$ADA 🚀🚀" spam post (no prose, no lean) — it must be DROPPED
    # by the noise gate (sub-min-length body, no explicit Bullish/Bearish label).
    client = FakeClient({"streams/symbol/ADA.X": _fx("stocktwits_noise.json")})
    adapter = StockTwitsAdapter(now=_now)
    items = adapter.fetch(client, {"tickers": ["ADA"], "min_body_chars": 25}, UNIVERSE)
    assert items, "expected at least the substantive posts to survive"
    # the cashtag/emoji-only spam (its only words are the cashtag itself) is gone
    assert all("🚀🚀" not in i.title for i in items)
    assert all(i.author != "cashtagspam" for i in items)


def test_stocktwits_noise_gate_keeps_short_LABELED_post() -> None:
    # A SHORT post ("$ADA") that would normally fail the length gate is KEPT when it carries an
    # explicit StockTwits Bullish/Bearish label — an explicit lean is signal even in a one-liner.
    client = FakeClient({"streams/symbol/ADA.X": _fx("stocktwits_noise.json")})
    adapter = StockTwitsAdapter(now=_now)
    items = adapter.fetch(client, {"tickers": ["ADA"], "min_body_chars": 25}, UNIVERSE)
    labeled = [i for i in items if i.author == "leanonly"]
    assert labeled, "short but explicitly-labeled post must survive the noise gate"
    assert labeled[0].engagement.get("sentiment") == "Bullish"


def test_stocktwits_multi_ticker_bait_does_not_fan_out() -> None:
    # A multi-cashtag pump post in the ADA stream lists bait tickers ($BTC $ETH $SOL) on top of the
    # promoted $ADA. It must NOT fan out to every ticker — tagging is restricted to the streamed
    # (substantive) ticker, ADA. (BTC/ETH/SOL are all in UNIVERSE, so a naive body-tag WOULD have
    # fanned the bait out to them.)
    client = FakeClient({"streams/symbol/ADA.X": _fx("stocktwits_noise.json")})
    adapter = StockTwitsAdapter(now=_now)
    items = adapter.fetch(client, {"tickers": ["ADA"], "min_body_chars": 25}, UNIVERSE)
    bait = next((i for i in items if i.author == "pumpcaller"), None)
    assert bait is not None, "the long-prose bait post should survive the length gate"
    assert bait.coins == ["ADA"], f"bait post fanned out to bait tickers: {bait.coins}"
    # and crucially the bait tickers never reached any other coin's digest via this post
    assert "BTC" not in bait.coins and "ETH" not in bait.coins and "SOL" not in bait.coins


def test_stocktwits_per_symbol_and_min_body_chars_knobs_read_from_config() -> None:
    # min_body_chars is configurable: a high threshold drops even moderate-length prose unless
    # labeled; per_symbol caps the messages considered per ticker.
    client = FakeClient({"streams/symbol/ADA.X": _fx("stocktwits_noise.json")})
    adapter = StockTwitsAdapter(now=_now)
    # huge min -> only the explicitly-labeled short post survives (everything else is "too short")
    items = adapter.fetch(client, {"tickers": ["ADA"], "min_body_chars": 500}, UNIVERSE)
    assert [i.author for i in items] == ["leanonly"]


# --------------------------------------------------------------------------- #
# telegram                                                                     #
# --------------------------------------------------------------------------- #


def test_telegram_adapter_extracts_messages() -> None:
    client = FakeClient({"t.me/s/cryptosignals": _fx("telegram.html")})
    adapter = TelegramAdapter(now=_now)
    items = adapter.fetch(client, {"channels": ["cryptosignals"]}, UNIVERSE)
    assert items
    assert all(i.source == "telegram" for i in items)
    # empty message block skipped
    assert all(i.body for i in items)
    assert "<" not in items[0].body  # HTML stripped
    coins = _coins(items)
    assert "BTC" in coins and "ETH" in coins and "SOL" in coins
    assert any("/cryptosignals/" in i.url for i in items)


def test_telegram_permalink_paired_with_own_message_when_non_text_precedes() -> None:
    # A photo message (no text div) precedes the text-bearing ones. The permalink for each text
    # block must come from its OWN enclosing message wrapper — not from a positional zip of all
    # data-post permalinks against only the text blocks (which would shift them by one).
    client = FakeClient({"t.me/s/cryptosignals": _fx("telegram_mixed.html")})
    adapter = TelegramAdapter(now=_now)
    items = adapter.fetch(client, {"channels": ["cryptosignals"]}, UNIVERSE)
    assert len(items) == 2  # the photo message yields no text item

    by_coin = {frozenset(i.coins): i for i in items}
    btc_eth = next(i for i in items if "BTC" in i.coins)
    sol = next(i for i in items if "SOL" in i.coins)
    # the BTC/ETH text lives in message 9002; the Solana text in 9003.
    assert btc_eth.url == "https://t.me/cryptosignals/9002"
    assert sol.url == "https://t.me/cryptosignals/9003"
    # and crucially the photo's permalink (9001) is NOT attributed to any text item.
    assert all("/9001" not in i.url for i in items)


def test_telegram_adapter_failsoft() -> None:
    adapter = TelegramAdapter(now=_now)
    cfg = {"channels": ["x"]}
    assert adapter.fetch(RaisingClient(), cfg, UNIVERSE) == []
    # malformed bytes -> no message divs -> [] (no raise)
    assert adapter.fetch(MalformedClient(), cfg, UNIVERSE) == []


# --------------------------------------------------------------------------- #
# youtube                                                                      #
# --------------------------------------------------------------------------- #


def test_youtube_adapter_uses_transcript() -> None:
    client = FakeClient(
        {
            "feeds/videos.xml": _fx("youtube_feed.xml"),
            "api/timedtext": _fx("timedtext.xml"),
        }
    )
    adapter = YouTubeAdapter(now=_now)
    items = adapter.fetch(client, {"channel_ids": ["UCabc123def456ghi789jkl"]}, UNIVERSE)
    assert items
    assert all(i.source == "youtube" for i in items)
    btc_vid = next(i for i in items if "outlook" in i.title.lower())
    assert btc_vid.engagement.get("has_transcript") is True
    assert "Bitcoin price action" in btc_vid.body  # transcript excerpt
    assert "BTC" in _coins(items) and "ETH" in _coins(items)


def test_youtube_adapter_falls_back_to_title_when_no_transcript() -> None:
    # timedtext route absent -> empty 200 -> body falls back to title + description
    client = FakeClient({"feeds/videos.xml": _fx("youtube_feed.xml")})
    adapter = YouTubeAdapter(now=_now)
    items = adapter.fetch(client, {"channel_ids": ["UCabc"]}, UNIVERSE)
    assert items
    assert all(i.engagement.get("has_transcript") is False for i in items)
    sol = next(i for i in items if "SOL" in i.coins)
    assert "Solana" in sol.body or "SOL" in sol.title


def test_youtube_adapter_failsoft() -> None:
    adapter = YouTubeAdapter(now=_now)
    cfg = {"channel_ids": ["UCabc"]}
    assert adapter.fetch(RaisingClient(), cfg, UNIVERSE) == []
    assert adapter.fetch(MalformedClient(), cfg, UNIVERSE) == []


# --------------------------------------------------------------------------- #
# forums                                                                       #
# --------------------------------------------------------------------------- #


def test_forums_adapter_returns_tagged_items() -> None:
    client = FakeClient({"bitcointalk": _fx("forums.rss.xml")})
    adapter = ForumsAdapter(now=_now)
    items = adapter.fetch(
        client, {"feeds": ["https://bitcointalk.org/index.php?board=1.0"]}, UNIVERSE
    )
    assert items
    assert all(i.source == "forums" for i in items)
    assert "BTC" in _coins(items) and "ADA" in _coins(items)


def test_forums_adapter_failsoft() -> None:
    adapter = ForumsAdapter(now=_now)
    cfg = {"forums_feeds": ["x"]}
    assert adapter.fetch(RaisingClient(), cfg, UNIVERSE) == []
    assert adapter.fetch(MalformedClient(), cfg, UNIVERSE) == []


def test_forums_does_not_mirror_the_shared_news_feeds() -> None:
    # REGRESSION: forums used to read the generic `feeds` key — the SHARED NEWS RSS list the crawler
    # hands every adapter — making it a duplicate news source. It must IGNORE that key and default
    # to its own Bitcointalk board feeds instead.
    shared_block = {
        "feeds": [
            "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
            "https://cointelegraph.com/rss",
        ],
        "subs": ["CryptoCurrency"],
    }
    client = FakeClient({"bitcointalk": _fx("forums.rss.xml")})
    adapter = ForumsAdapter(now=_now)
    items = adapter.fetch(client, shared_block, UNIVERSE)
    # forums hit ONLY its own Bitcointalk defaults — never the news feeds.
    assert client.calls, "forums made no requests"
    assert all("bitcointalk" in u for u in client.calls)
    assert not any("coindesk" in u or "cointelegraph" in u for u in client.calls)
    # and it still parses its own forum fixture into tagged items.
    assert items and all(i.source == "forums" for i in items)
    assert "BTC" in _coins(items) and "ADA" in _coins(items)


def test_forums_uses_its_own_feeds_when_configured() -> None:
    # An explicit forums-specific feed list (either `forums_feeds` or a nested `forums.feeds`
    # block) is honoured over the Bitcointalk default — and over the shared news `feeds`.
    client = FakeClient({"myforum": _fx("forums.rss.xml")})
    adapter = ForumsAdapter(now=_now)
    cfg = {"feeds": ["https://cointelegraph.com/rss"],
           "forums": {"feeds": ["https://myforum.org/board/rss"]}}
    items = adapter.fetch(client, cfg, UNIVERSE)
    assert client.calls == ["https://myforum.org/board/rss"]
    assert items and all(i.source == "forums" for i in items)


# --------------------------------------------------------------------------- #
# registry                                                                     #
# --------------------------------------------------------------------------- #


def test_registry_has_all_sources() -> None:
    expected = {"rss", "reddit", "nitter", "stocktwits", "telegram", "youtube", "forums"}
    assert expected <= set(SOURCE_ADAPTERS)
    assert SOURCE_ADAPTERS["rss"].name == "rss"


def test_enabled_adapters_filters_by_config_block() -> None:
    chosen = enabled_adapters({"sources": ["rss", "reddit"]})
    names = [a.name for a in chosen]
    assert names == ["rss", "reddit"]


def test_enabled_adapters_ignores_unknown_names() -> None:
    chosen = enabled_adapters({"sources": ["rss", "does_not_exist"]})
    assert [a.name for a in chosen] == ["rss"]


def test_enabled_adapters_default_when_no_block() -> None:
    chosen = enabled_adapters({})
    assert len(chosen) == len(SOURCE_ADAPTERS)


@pytest.mark.parametrize(
    "bad_cfg",
    [
        {"sources": 123},          # non-iterable scalar
        {"sources": "rss"},        # a bare string (iterating it yields chars, not names)
        {"sources": 3.14},
        {"sources": object()},
        {"sources": {"rss": 1}},   # a dict, not a list of names
        {"sources": True},
        123,                        # cfg itself is garbage (cfg_get tolerates -> None block)
        "garbage",
        None,
    ],
)
def test_enabled_adapters_failsoft_on_malformed_sources_block(bad_cfg) -> None:
    # A malformed/garbage 'sources' block (or cfg) must NEVER raise — enabled_adapters runs BEFORE
    # the protected pool in crawl_tick, so a raise here aborts the whole tick. Fall back to the
    # sane default set (every registered enabled adapter) instead.
    chosen = enabled_adapters(bad_cfg)
    assert [a.name for a in chosen] == [a.name for a in SOURCE_ADAPTERS.values() if a.enabled]


def test_enabled_adapters_list_with_non_string_names_is_tolerated() -> None:
    # A list that mixes valid names with junk entries keeps the valid ones, drops the junk, no raise.
    chosen = enabled_adapters({"sources": ["rss", 123, None, "reddit", {"x": 1}]})
    assert [a.name for a in chosen] == ["rss", "reddit"]


def test_build_adapters_injects_clock() -> None:
    reg = build_adapters(now=_now)
    client = FakeClient({"coindesk": _fx("news.rss.xml")})
    items = reg["rss"].fetch(client, {"feeds": ["https://www.coindesk.com/rss"]}, UNIVERSE)
    assert items and all(i.fetched_ts == NOW for i in items)


@pytest.mark.parametrize("adapter", list(SOURCE_ADAPTERS.values()))
def test_every_adapter_failsoft_on_raising_client(adapter) -> None:
    # broad cfg so each adapter has at least one target to try
    cfg = {
        "feeds": ["x"],
        "subreddits": ["x"],
        "mirrors": ["https://nitter.net"],
        "handles": ["x"],
        "channels": ["x"],
        "channel_ids": ["UCx"],
        "tickers": ["BTC"],
    }
    assert adapter.fetch(RaisingClient(), cfg, UNIVERSE) == []
    assert adapter.fetch(None, cfg, UNIVERSE) == []
