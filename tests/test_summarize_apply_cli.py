"""Offline tests for scripts/summarize_apply_cli.py (the sole writer of LLM summaries).

We pre-store unsummarized items, hand the CLI a canned summarizer output, and assert it
DETERMINISTICALLY folds each valid verdict into the store (summary + item_sentiment + summarized_ts
on the day-file record AND the per-coin index pointer), then refreshes the affected coins' digests
so rolling_s moves. We assert it REJECTS an invalid sentiment label (skip + logged, store
untouched), and skips unknown ids / already-summarized items.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund import content_store
from futures_fund.content_store import ContentItem, get_item, make_id
from scripts.summarize_apply_cli import apply_summaries

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _item(*, url, title, coins, sentiment=None, published=NOW):
    return ContentItem(
        id=make_id("rss", url, title),
        source="rss", feed="rss-feed", url=url, title=title, body="body text",
        published_ts=published, fetched_ts=published, coins=coins, item_sentiment=sentiment,
    )


def _store(content_dir, items):
    content_store.store_items(content_dir, items)


# --------------------------------------------------------------------------- happy path


def test_applies_valid_summary_and_updates_item(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="BTC breaks out", coins=["BTC"])
    _store(content_dir, [it])

    out = apply_summaries(content_dir, [
        {"item_id": it.id, "summary": "Bullish breakout above resistance.",
         "item_sentiment": "very_positive"},
    ], NOW)

    assert out["applied"] == 1 and out["skipped"] == 0
    stored = get_item(content_dir, it.id)
    assert stored.item_sentiment == "very_positive"
    assert stored.summary == "Bullish breakout above resistance."
    assert stored.summarized_ts is not None
    assert content_store._as_utc(stored.summarized_ts) == NOW


def test_index_pointer_sentiment_synced(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="BTC dip", coins=["BTC"])
    _store(content_dir, [it])

    apply_summaries(content_dir, [
        {"item_id": it.id, "summary": "s", "item_sentiment": "negative"},
    ], NOW)

    ptrs = content_store.index_for_coin(content_dir, "BTC", NOW - content_store._days(1))
    assert any(p["item_id"] == it.id and p["item_sentiment"] == "negative" for p in ptrs)


def test_digest_updated_for_affected_coins(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="ETH surges", coins=["ETH"])
    _store(content_dir, [it])
    # before: no digest, or rolling_s == 0
    apply_summaries(content_dir, [
        {"item_id": it.id, "summary": "s", "item_sentiment": "very_positive"},
    ], NOW)

    from futures_fund.decision_io import read_digest
    dig = read_digest(content_dir, "ETH")
    assert dig["coin"] == "ETH"
    assert dig["rolling_s"] > 0.0          # a positive verdict moved the rolling score up


def test_multi_coin_item_updates_each_digest_once(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://m", title="BTC and ETH rally", coins=["BTC", "ETH"])
    _store(content_dir, [it])
    out = apply_summaries(content_dir, [
        {"item_id": it.id, "summary": "s", "item_sentiment": "positive"},
    ], NOW)
    assert out["coins_updated"] == ["BTC", "ETH"]
    from futures_fund.decision_io import read_digest
    assert read_digest(content_dir, "BTC")["rolling_s"] > 0.0
    assert read_digest(content_dir, "ETH")["rolling_s"] > 0.0


# --------------------------------------------------------------------------- rejection


def test_rejects_invalid_sentiment_label(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="BTC news", coins=["BTC"])
    _store(content_dir, [it])

    out = apply_summaries(content_dir, [
        {"item_id": it.id, "summary": "s", "item_sentiment": "mega_bullish"},  # not a level
    ], NOW)

    assert out["applied"] == 0 and out["skipped"] == 1
    assert any("invalid item_sentiment" in e for e in out["errors"])
    # store untouched
    stored = get_item(content_dir, it.id)
    assert stored.item_sentiment is None
    assert stored.summary is None


def test_rejects_non_string_garbage_sentiment(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="BTC news", coins=["BTC"])
    _store(content_dir, [it])
    for bad in (None, 5, "", "POSITIVE", "bull"):
        out = apply_summaries(content_dir, [
            {"item_id": it.id, "summary": "s", "item_sentiment": bad},
        ], NOW)
        assert out["applied"] == 0 and out["skipped"] == 1
    assert get_item(content_dir, it.id).item_sentiment is None


def test_skips_unknown_item_id(tmp_path):
    content_dir = tmp_path / "content"
    out = apply_summaries(content_dir, [
        {"item_id": "does-not-exist", "summary": "s", "item_sentiment": "positive"},
    ], NOW)
    assert out["applied"] == 0 and out["skipped"] == 1
    assert any("unknown item_id" in e for e in out["errors"])


def test_skips_already_summarized_item(tmp_path):
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="BTC", coins=["BTC"], sentiment="neutral")
    _store(content_dir, [it])
    out = apply_summaries(content_dir, [
        {"item_id": it.id, "summary": "new", "item_sentiment": "very_positive"},
    ], NOW)
    assert out["applied"] == 0 and out["skipped"] == 1
    assert get_item(content_dir, it.id).item_sentiment == "neutral"  # left intact


def test_mixed_batch_applies_valid_skips_invalid(tmp_path):
    content_dir = tmp_path / "content"
    a = _item(url="http://a", title="BTC up", coins=["BTC"])
    b = _item(url="http://b", title="ETH down", coins=["ETH"])
    _store(content_dir, [a, b])

    out = apply_summaries(content_dir, [
        {"item_id": a.id, "summary": "ok", "item_sentiment": "positive"},
        {"item_id": b.id, "summary": "bad", "item_sentiment": "INVALID"},
        "not even a dict",
        {"summary": "no id", "item_sentiment": "neutral"},
    ], NOW)

    assert out["applied"] == 1 and out["skipped"] == 3
    assert get_item(content_dir, a.id).item_sentiment == "positive"
    assert get_item(content_dir, b.id).item_sentiment is None
    assert out["coins_updated"] == ["BTC"]


def test_all_five_levels_accepted(tmp_path):
    content_dir = tmp_path / "content"
    levels = ["very_positive", "positive", "neutral", "negative", "very_negative"]
    items = [_item(url=f"http://{lv}", title=f"t-{lv}", coins=["BTC"]) for lv in levels]
    _store(content_dir, items)
    rows = [{"item_id": it.id, "summary": "s", "item_sentiment": lv}
            for it, lv in zip(items, levels, strict=True)]
    out = apply_summaries(content_dir, rows, NOW)
    assert out["applied"] == 5 and out["skipped"] == 0
    for it, lv in zip(items, levels, strict=True):
        assert get_item(content_dir, it.id).item_sentiment == lv


# --------------------------------------------------------------------------- file loader / main


def test_main_rejects_non_list_json(tmp_path):
    from scripts.summarize_apply_cli import main
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"item_id": "x"}))   # object, not a list (and no `summaries` wrapper)
    assert main([str(bad), "--content-dir", str(tmp_path / "content")]) == 1


def test_main_accepts_summaries_wrapper(tmp_path):
    from scripts.summarize_apply_cli import main
    content_dir = tmp_path / "content"
    it = _item(url="http://a", title="BTC", coins=["BTC"])
    _store(content_dir, [it])
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"summaries": [
        {"item_id": it.id, "summary": "s", "item_sentiment": "positive"}]}))
    assert main([str(f), "--content-dir", str(content_dir)]) == 0
    assert get_item(content_dir, it.id).item_sentiment == "positive"


def test_main_bad_path_returns_1(tmp_path):
    from scripts.summarize_apply_cli import main
    assert main([str(tmp_path / "nope.json"), "--content-dir", str(tmp_path / "content")]) == 1
