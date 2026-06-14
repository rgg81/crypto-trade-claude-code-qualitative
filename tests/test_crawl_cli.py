"""Offline tests for scripts/crawl_cli.py (the 15-minute crawl tick driver).

No live network: a FAKE crawl_tick / FAKE client is injected. We assert the WORKING UNIVERSE is
built as (config core_watchlist + attention-spiking coins from the on-disk digests), the real
client is threaded into the tick, content/_pending_summaries.json is written with exactly the items
still lacking a summary (compact {item_id, source, title, body_excerpt, coins} rows), and the
15-min slot is stamped so a re-poll SKIPs.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund import content_store
from futures_fund.content_store import ContentItem, make_id
from futures_fund.crawler import CrawlResult
from scripts.crawl_cli import run_crawl

NOW = datetime(2026, 6, 13, 12, 7, 0, tzinfo=UTC)


def _settings(core, *, ratio=2.0, min_mentions=5, interval_min=15):
    from futures_fund.config import QualitativeSettings, Settings, SourcesSettings
    return Settings(
        qualitative=QualitativeSettings(
            core_watchlist=core, spike_ratio=ratio,
            spike_min_mentions=min_mentions, crawl_interval_min=interval_min,
        ),
        sources=SourcesSettings(enabled=["rss", "reddit"], feeds=["http://feed"], subs=["sub"]),
    )


def _item(*, source, url, title, coins, body="", sentiment=None, published=NOW):
    return ContentItem(
        id=make_id(source, url, title),
        source=source, feed=f"{source}-feed", url=url, title=title, body=body,
        published_ts=published, fetched_ts=published, coins=coins, item_sentiment=sentiment,
    )


def _write_digest(content_dir, coin, *, vol_24h, baseline):
    content_store._atomic_write_text(
        content_store._digest_path(content_dir, coin),
        json.dumps({"coin": coin.upper(), "mention_volume_24h": vol_24h,
                    "mention_volume_baseline": baseline}),
    )


# --------------------------------------------------------------------------- universe


def test_universe_is_core_only_when_no_spikes(tmp_path):
    captured = {}

    def fake_crawl(content_dir, state_dir, cfg, universe, now, client=None):
        captured["universe"] = universe
        captured["cfg"] = cfg
        captured["client"] = client
        return CrawlResult(new_items=0)

    out = run_crawl(
        state_dir=str(tmp_path / "state"), content_dir=str(tmp_path / "content"),
        now=NOW, client=object(), crawl_fn=fake_crawl, settings=_settings(["BTC", "ETH"]),
    )
    assert captured["universe"] == ["BTC", "ETH"]
    assert out["universe"]["core"] == ["BTC", "ETH"]
    assert out["universe"]["spiking"] == []


def test_universe_adds_attention_spikes_from_digests(tmp_path):
    content_dir = tmp_path / "content"
    # PEPE is not in core but spikes hard; BTC is core (always present); QUIET does not spike.
    _write_digest(content_dir, "PEPE", vol_24h=40, baseline=1.0)
    _write_digest(content_dir, "QUIET", vol_24h=1, baseline=1.0)

    captured = {}

    def fake_crawl(content_dir_, state_dir, cfg, universe, now, client=None):
        captured["universe"] = universe
        return CrawlResult(new_items=0)

    out = run_crawl(
        state_dir=str(tmp_path / "state"), content_dir=str(content_dir),
        now=NOW, client=object(), crawl_fn=fake_crawl, settings=_settings(["BTC", "ETH"]),
    )
    assert "PEPE" in captured["universe"]      # spiking add-in
    assert "BTC" in captured["universe"]       # core always present
    assert "QUIET" not in captured["universe"]
    assert out["universe"]["spiking"] == ["PEPE"]


def test_cfg_has_enabled_names_and_per_source_config(tmp_path):
    captured = {}

    def fake_crawl(content_dir, state_dir, cfg, universe, now, client=None):
        captured["cfg"] = cfg
        return CrawlResult(new_items=0)

    run_crawl(
        state_dir=str(tmp_path / "state"), content_dir=str(tmp_path / "content"),
        now=NOW, client=object(), crawl_fn=fake_crawl, settings=_settings(["BTC"]),
    )
    cfg = captured["cfg"]
    assert cfg["sources"] == ["rss", "reddit"]            # enabled-adapter NAME list
    assert cfg["sources_config"]["rss"]["feeds"] == ["http://feed"]
    assert cfg["sources_config"]["reddit"]["subs"] == ["sub"]


# --------------------------------------------------------------------------- client


def test_client_factory_used_and_closed(tmp_path):
    closed = {"v": False}

    class FakeClient:
        def close(self):
            closed["v"] = True

    fc = FakeClient()
    seen = {}

    def factory(timeout_s):
        seen["timeout_s"] = timeout_s
        return fc

    def fake_crawl(content_dir, state_dir, cfg, universe, now, client=None):
        seen["client_is_fc"] = client is fc
        return CrawlResult(new_items=0)

    run_crawl(
        state_dir=str(tmp_path / "state"), content_dir=str(tmp_path / "content"),
        now=NOW, client_factory=factory, crawl_fn=fake_crawl, settings=_settings(["BTC"]),
    )
    assert seen["client_is_fc"] is True
    assert seen["timeout_s"] == 8.0
    assert closed["v"] is True   # built client always closed


# --------------------------------------------------------------------------- pending


def test_pending_summaries_written_for_unsummarized_items(tmp_path):
    content_dir = tmp_path / "content"
    # Pre-store: one summarized (excluded) + two not-yet-summarized (included).
    content_store.store_items(content_dir, [
        _item(source="rss", url="http://a", title="BTC pump", coins=["BTC"],
              body="x" * 5000, sentiment=None),
        _item(source="rss", url="http://b", title="ETH news", coins=["ETH"], sentiment=None),
        _item(source="rss", url="http://c", title="done", coins=["BTC"], sentiment="positive"),
    ])

    def fake_crawl(content_dir_, state_dir, cfg, universe, now, client=None):
        return CrawlResult(new_items=0)   # store untouched; collect from what's already there

    out = run_crawl(
        state_dir=str(tmp_path / "state"), content_dir=str(content_dir),
        now=NOW, client=object(), crawl_fn=fake_crawl, settings=_settings(["BTC", "ETH"]),
    )

    pending = json.loads((content_dir / "_pending_summaries.json").read_text())
    titles = {p["title"] for p in pending}
    assert titles == {"BTC pump", "ETH news"}             # summarized item excluded
    row = next(p for p in pending if p["title"] == "BTC pump")
    assert set(row) == {"item_id", "source", "title", "body_excerpt", "coins"}
    assert len(row["body_excerpt"]) <= 600                 # body trimmed to excerpt
    assert row["coins"] == ["BTC"]
    assert out["pending"] == pending


def test_pending_deduped_across_coins(tmp_path):
    content_dir = tmp_path / "content"
    # one item tagged to TWO universe coins must appear ONCE in the pending queue.
    content_store.store_items(content_dir, [
        _item(source="rss", url="http://multi", title="BTC & ETH rally", coins=["BTC", "ETH"]),
    ])

    def fake_crawl(content_dir_, state_dir, cfg, universe, now, client=None):
        return CrawlResult(new_items=0)

    run_crawl(
        state_dir=str(tmp_path / "state"), content_dir=str(content_dir),
        now=NOW, client=object(), crawl_fn=fake_crawl, settings=_settings(["BTC", "ETH"]),
    )
    pending = json.loads((content_dir / "_pending_summaries.json").read_text())
    assert len(pending) == 1
    assert sorted(pending[0]["coins"]) == ["BTC", "ETH"]


# --------------------------------------------------------------------------- stamp


def test_crawl_stamps_slot_so_repoll_skips(tmp_path):
    from futures_fund.scheduling import crawl_due

    state_dir = tmp_path / "state"

    def fake_crawl(content_dir, state_dir_, cfg, universe, now, client=None):
        return CrawlResult(new_items=0)

    assert crawl_due(state_dir, NOW)[0] == "DUE"   # before
    run_crawl(
        state_dir=str(state_dir), content_dir=str(tmp_path / "content"),
        now=NOW, client=object(), crawl_fn=fake_crawl, settings=_settings(["BTC"]),
    )
    # same 15-min slot now SKIPs
    assert crawl_due(state_dir, datetime(2026, 6, 13, 12, 14, 0, tzinfo=UTC))[0] == "SKIP"
    # next slot is DUE again
    assert crawl_due(state_dir, datetime(2026, 6, 13, 12, 16, 0, tzinfo=UTC))[0] == "DUE"
