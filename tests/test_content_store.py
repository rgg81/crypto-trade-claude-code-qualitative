from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from futures_fund.content_store import (
    ContentItem,
    canonical_url,
    get_item,
    index_for_coin,
    make_id,
    normalize_title,
    purge,
    store_items,
    update_digest,
)
from futures_fund.sentiment_decay import decay_score, level_to_s

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _item(
    *,
    source: str = "coindesk",
    feed: str = "https://coindesk.com/rss",
    url: str = "https://coindesk.com/a/1",
    title: str = "Bitcoin rips higher",
    body: str = "",
    author: str = "",
    coins: list[str] | None = None,
    published_ts: datetime = NOW,
    fetched_ts: datetime = NOW,
    engagement: dict | None = None,
    item_sentiment=None,
) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title),
        source=source,
        feed=feed,
        url=url,
        title=title,
        body=body,
        author=author,
        coins=coins if coins is not None else ["BTC"],
        published_ts=published_ts,
        fetched_ts=fetched_ts,
        engagement=engagement or {},
        item_sentiment=item_sentiment,
    )


# --- canonicalisation / id ---------------------------------------------------


def test_canonical_url_strips_query_and_trailing_slash() -> None:
    base = "https://x.com/a/post"
    assert canonical_url("https://x.com/a/post/") == base
    assert canonical_url("https://x.com/a/post?utm_source=tw&utm_medium=x") == base
    assert canonical_url("https://x.com/a/post#section") == base
    assert canonical_url("https://x.com/a/post/?ref=home") == base
    assert canonical_url("") == ""


def test_normalize_title_lowercases_and_collapses_ws() -> None:
    assert normalize_title("  Bitcoin   RIPS\nhigher ") == "bitcoin rips higher"
    assert normalize_title("") == ""


def test_make_id_is_sha1_hex_and_stable() -> None:
    a = make_id("coindesk", "https://x.com/a?utm_source=tw", "Bitcoin Rips")
    b = make_id("coindesk", "https://x.com/a/", "  bitcoin   rips ")
    assert a == b  # same canonical url + normalized title
    assert len(a) == 40 and all(c in "0123456789abcdef" for c in a)


# --- store_items dedupe ------------------------------------------------------


def test_store_returns_only_new_and_partitions_by_day(tmp_path: Path) -> None:
    cd = str(tmp_path)
    day_old = NOW - timedelta(days=3)
    items = [
        _item(url="https://coindesk.com/a/1", title="A", published_ts=NOW),
        _item(url="https://coindesk.com/a/2", title="B", published_ts=day_old),
    ]
    new = store_items(cd, items)
    assert len(new) == 2

    # day partitioning by published_ts UTC date
    assert (tmp_path / "items" / f"items-{NOW.date().isoformat()}.jsonl").exists()
    assert (tmp_path / "items" / f"items-{day_old.date().isoformat()}.jsonl").exists()

    # re-storing the same items returns nothing new (dedupe against day file)
    assert store_items(cd, items) == []


def test_dedupe_same_url_reposts(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # same article, three tracking variants -> one stored item
    repost_a = _item(url="https://news.com/x", title="Same Story")
    repost_b = _item(url="https://news.com/x?utm_source=twitter", title="Same Story")
    repost_c = _item(url="https://news.com/x/", title="Same   Story")
    new = store_items(cd, [repost_a, repost_b, repost_c])
    assert len(new) == 1


def test_dedupe_cross_source_duplicate_titles_when_no_url(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # two feeds carrying the identical headline with no permalink collapse via normalized title
    a = _item(source="aggregator", url="", title="ETF Approved Today")
    b = _item(source="aggregator", url="", title="etf  approved   today")
    new = store_items(cd, [a, b])
    assert len(new) == 1


def test_dedupe_same_url_across_sources_collapses_to_one(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # the SAME article reaches the desk under two source names (rss + forums) with the same
    # canonical url. It must collapse to ONE stored item regardless of source — otherwise the
    # crowd read double-counts the same story.
    rss = _item(source="rss", url="https://news.com/etf", title="ETF Approved", body="short")
    forums = _item(source="forums", url="https://news.com/etf?utm_source=fb",
                   title="ETF Approved", body="much longer richer body text here")
    new = store_items(cd, [rss, forums])
    assert len(new) == 1
    # the richer (longer-body) copy is the one kept
    kept = new[0]
    assert kept.body == "much longer richer body text here"
    assert get_item(cd, kept.id) is not None
    # only one pointer is indexed for BTC across both sources
    ptrs = index_for_coin(cd, "BTC", NOW - timedelta(days=1))
    assert len(ptrs) == 1


def test_dedupe_same_url_across_sources_across_calls(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # first store the rss copy, then a later crawl brings the same url under "forums":
    # the second call must store NOTHING new (same canonical url, different source).
    rss = _item(source="rss", url="https://news.com/dup", title="Dup", body="short")
    assert len(store_items(cd, [rss])) == 1
    forums = _item(source="forums", url="https://news.com/dup", title="Dup", body="short")
    assert store_items(cd, [forums]) == []


def test_store_dedupes_repeated_coins_per_item(tmp_path: Path) -> None:
    # An item tagged with the same coin twice (coins=['BTC','BTC']) must write ONE pointer, not two,
    # so it cannot double-count in update_digest's mention_volume / n_items_30d.
    cd = str(tmp_path)
    it = _item(url="https://n/dupcoin", title="dup coin", coins=["BTC", "BTC", "btc"])
    store_items(cd, [it])

    ptrs = index_for_coin(cd, "BTC", NOW - timedelta(days=1))
    assert [p["item_id"] for p in ptrs] == [it.id]  # exactly one pointer for this item

    digest = update_digest(cd, "BTC", NOW)
    assert digest["n_items_30d"] == 1
    assert digest["mention_volume_24h"] == 1


def test_dedupe_within_single_batch(tmp_path: Path) -> None:
    cd = str(tmp_path)
    dup = _item(url="https://news.com/y", title="Dup")
    dup2 = _item(url="https://news.com/y?utm_x=1", title="Dup")
    new = store_items(cd, [dup, dup2])
    assert len(new) == 1


# --- get_item ----------------------------------------------------------------


def test_get_item_round_trips(tmp_path: Path) -> None:
    cd = str(tmp_path)
    it = _item(
        url="https://news.com/z",
        title="Round trip",
        body="full body text",
        author="reporter",
        coins=["BTC", "ETH"],
        engagement={"score": 42},
        item_sentiment="positive",
    )
    store_items(cd, [it])
    got = get_item(cd, it.id)
    assert got is not None
    assert got.id == it.id
    assert got.title == "Round trip"
    assert got.body == "full body text"
    assert got.author == "reporter"
    assert got.coins == ["BTC", "ETH"]
    assert got.engagement == {"score": 42}
    assert got.item_sentiment == "positive"


def test_get_item_unknown_returns_none(tmp_path: Path) -> None:
    assert get_item(str(tmp_path), "deadbeef") is None


# --- index_for_coin ----------------------------------------------------------


def test_index_for_coin_respects_since(tmp_path: Path) -> None:
    cd = str(tmp_path)
    recent = _item(url="https://n/1", title="recent", published_ts=NOW - timedelta(hours=2))
    old = _item(url="https://n/2", title="old", published_ts=NOW - timedelta(days=10))
    store_items(cd, [recent, old])

    since = NOW - timedelta(days=1)
    ptrs = index_for_coin(cd, "BTC", since)
    ids = {p["item_id"] for p in ptrs}
    assert recent.id in ids
    assert old.id not in ids

    # since at the boundary is inclusive
    boundary = index_for_coin(cd, "BTC", recent.published_ts)
    assert recent.id in {p["item_id"] for p in boundary}


def test_index_indexes_every_tagged_coin(tmp_path: Path) -> None:
    cd = str(tmp_path)
    it = _item(url="https://n/multi", title="multi", coins=["BTC", "ETH"])
    store_items(cd, [it])
    assert it.id in {p["item_id"] for p in index_for_coin(cd, "BTC", NOW - timedelta(days=1))}
    assert it.id in {p["item_id"] for p in index_for_coin(cd, "ETH", NOW - timedelta(days=1))}


# --- update_digest -----------------------------------------------------------


def test_update_digest_decay_matches_sentiment_decay(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # one item at age 0 (very_positive) and one one-half-life old (very_positive)
    fresh = _item(
        url="https://n/fresh", title="fresh bull", published_ts=NOW, item_sentiment="very_positive"
    )
    half_life_old = _item(
        url="https://n/old",
        title="aged bull",
        published_ts=NOW - timedelta(days=2.5),  # exactly one half-life
        item_sentiment="very_positive",
    )
    store_items(cd, [fresh, half_life_old])

    digest = update_digest(cd, "BTC", NOW, half_life_days=2.5)

    expected = decay_score(level_to_s("very_positive"), 0.0, 2.5) + decay_score(
        level_to_s("very_positive"), 2.5 * 24.0, 2.5
    )
    assert digest["rolling_s"] == pytest.approx(expected)
    assert digest["rolling_s"] == pytest.approx(1.0 + 0.5)
    assert digest["n_items_30d"] == 2

    # persisted to disk
    on_disk = json.loads((tmp_path / "digests" / "BTC.json").read_text())
    assert on_disk["rolling_s"] == pytest.approx(expected)
    assert on_disk["coin"] == "BTC"
    assert on_disk["one_line"] == ""


def test_update_digest_none_sentiment_contributes_zero(tmp_path: Path) -> None:
    cd = str(tmp_path)
    rated = _item(url="https://n/r", title="rated", item_sentiment="positive", published_ts=NOW)
    unrated = _item(url="https://n/u", title="unrated", item_sentiment=None, published_ts=NOW)
    store_items(cd, [rated, unrated])
    digest = update_digest(cd, "BTC", NOW)
    assert digest["rolling_s"] == pytest.approx(level_to_s("positive"))
    assert digest["n_items_30d"] == 2


def test_update_digest_volume_and_breakdown(tmp_path: Path) -> None:
    cd = str(tmp_path)
    h1 = NOW - timedelta(hours=1)
    h5 = NOW - timedelta(hours=5)
    d4 = NOW - timedelta(days=4)
    items = [
        _item(source="coindesk", url="https://n/a", title="a", published_ts=h1),
        _item(source="coindesk", url="https://n/b", title="b", published_ts=h5),
        _item(source="reddit", url="https://n/c", title="c", published_ts=d4),
    ]
    store_items(cd, items)
    digest = update_digest(cd, "BTC", NOW)
    assert digest["mention_volume_24h"] == 2  # the two within 24h
    assert digest["n_items_30d"] == 3
    # span of stored data is 4 days (oldest item 4d ago) -> baseline divides by 4, not 30
    assert digest["mention_volume_baseline"] == pytest.approx(3 / 4.0)
    assert digest["source_breakdown"] == {"coindesk": 2, "reddit": 1}


def test_update_digest_baseline_reflects_actual_span(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # 100 items spread over the last 5 days -> baseline ~20/day, NOT 100/30 ~ 3.3/day.
    # Dividing by the full 30d window when the store only holds 5d of data would deflate the
    # baseline ~6x and make every coin "spike".
    items = []
    for i in range(100):
        age_hours = (i / 99.0) * (5 * 24)  # spread across the trailing 5 days
        items.append(
            _item(
                source="rss",
                url=f"https://n/span/{i}",
                title=f"span {i}",
                published_ts=NOW - timedelta(hours=age_hours),
            )
        )
    store_items(cd, items)
    digest = update_digest(cd, "BTC", NOW)
    assert digest["n_items_30d"] == 100
    # oldest item is 5 days back -> ceil(5) = 5 day span -> 100/5 == 20/day
    assert digest["mention_volume_baseline"] == pytest.approx(20.0)


def test_update_digest_baseline_caps_span_at_window(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # a near-full-window store (oldest item 29d back) divides by its true span (29 days), and the
    # span can never exceed the 30-day window. With a single fresh item the floor is 1 day.
    items = [
        _item(url="https://n/edge", title="edge", published_ts=NOW - timedelta(days=29)),
        _item(url="https://n/fresh", title="fresh", published_ts=NOW),
    ]
    store_items(cd, items)
    digest = update_digest(cd, "BTC", NOW)
    assert digest["mention_volume_baseline"] == pytest.approx(2 / 29.0)

    # single fresh item -> span floored at 1 day, not a fractional sub-day divisor
    cd2 = str(tmp_path / "two")
    store_items(cd2, [_item(url="https://n/only", title="only", published_ts=NOW)])
    digest2 = update_digest(cd2, "BTC", NOW)
    assert digest2["mention_volume_baseline"] == pytest.approx(1.0)


def test_update_digest_top_item_ids_ranked_by_engagement(tmp_path: Path) -> None:
    cd = str(tmp_path)
    hot = _item(url="https://n/hot", title="hot", engagement={"score": 1000})
    cold = _item(url="https://n/cold", title="cold", engagement={"score": 1})
    store_items(cd, [cold, hot])
    digest = update_digest(cd, "BTC", NOW)
    assert digest["top_item_ids"][0] == hot.id


def test_update_digest_top_item_ids_demotes_stale_viral(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # a fresh, medium-engagement item must outrank a stale (long-ago, multiple half-lives)
    # high-engagement item: its catalyst is priced in, so decay-weighted engagement demotes it.
    stale_viral = _item(
        url="https://n/staleviral",
        title="stale viral",
        engagement={"score": 1000},
        published_ts=NOW - timedelta(days=10),  # ~4 half-lives -> heavily decayed
    )
    fresh_medium = _item(
        url="https://n/freshmed",
        title="fresh medium",
        engagement={"score": 100},
        published_ts=NOW,
    )
    store_items(cd, [stale_viral, fresh_medium])
    digest = update_digest(cd, "BTC", NOW)
    assert digest["top_item_ids"][0] == fresh_medium.id
    # both still present, just reordered
    assert set(digest["top_item_ids"]) == {fresh_medium.id, stale_viral.id}


def test_update_digest_empty_coin(tmp_path: Path) -> None:
    digest = update_digest(str(tmp_path), "DOGE", NOW)
    assert digest["rolling_s"] == 0.0
    assert digest["n_items_30d"] == 0
    assert digest["top_item_ids"] == []


# --- purge -------------------------------------------------------------------


def test_purge_deletes_old_day_files_and_trims_index(tmp_path: Path) -> None:
    cd = str(tmp_path)
    recent = _item(url="https://n/recent", title="recent", published_ts=NOW - timedelta(days=2))
    old = _item(url="https://n/old", title="old", published_ts=NOW - timedelta(days=40))
    store_items(cd, [recent, old])

    # both day files + both pointers exist before purge
    assert len(index_for_coin(cd, "BTC", NOW - timedelta(days=365))) == 2

    result = purge(cd, NOW, retain_days=30)
    assert result["files_deleted"] == 1
    assert result["pointers_dropped"] == 1

    # old day file gone, recent kept
    old_day = tmp_path / "items" / f"items-{old.published_ts.date().isoformat()}.jsonl"
    recent_day = tmp_path / "items" / f"items-{recent.published_ts.date().isoformat()}.jsonl"
    assert not old_day.exists()
    assert recent_day.exists()

    # index trimmed to the recent pointer only
    ptrs = index_for_coin(cd, "BTC", NOW - timedelta(days=365))
    assert {p["item_id"] for p in ptrs} == {recent.id}

    # recent item still retrievable, old one gone
    assert get_item(cd, recent.id) is not None
    assert get_item(cd, old.id) is None


def test_purge_keeps_everything_within_window(tmp_path: Path) -> None:
    cd = str(tmp_path)
    items = [
        _item(url="https://n/1", title="1", published_ts=NOW - timedelta(days=1)),
        _item(url="https://n/2", title="2", published_ts=NOW - timedelta(days=29)),
    ]
    store_items(cd, items)
    result = purge(cd, NOW, retain_days=30)
    assert result == {"files_deleted": 0, "pointers_dropped": 0}
    assert len(index_for_coin(cd, "BTC", NOW - timedelta(days=365))) == 2


def test_purge_empty_store_is_noop(tmp_path: Path) -> None:
    assert purge(str(tmp_path), NOW) == {"files_deleted": 0, "pointers_dropped": 0}


def test_purge_never_orphans_item_on_cutoff_day(tmp_path: Path) -> None:
    # An item on the SAME calendar day as the cutoff but before the cutoff TIME must be treated
    # identically by both deletions: either fully kept or fully gone. The invariant is that a body
    # retrievable via get_item is ALSO visible via index_for_coin (no orphaned in-window pointers).
    cd = str(tmp_path)
    # cutoff = NOW - 30d = 2026-05-14 12:00 (cutoff_day = 2026-05-14).
    boundary = datetime(2026, 5, 14, 6, 0, 0, tzinfo=UTC)  # same day, before cutoff time
    it = _item(url="https://n/boundary", title="boundary", published_ts=boundary)
    store_items(cd, [it])

    purge(cd, NOW, retain_days=30)

    in_index = it.id in {p["item_id"] for p in index_for_coin(cd, "BTC", NOW - timedelta(days=365))}
    in_body = get_item(cd, it.id) is not None
    # get_item and index_for_coin must AGREE — no orphan (body kept while pointer dropped).
    assert in_body == in_index


def test_append_after_torn_line_never_loses_a_valid_record(tmp_path: Path) -> None:
    # Inject a torn (newline-less) trailing line into a day file, then append another item via
    # store_items. Every previously-stored id must remain retrievable via get_item (no silent loss
    # from a torn line concatenating with the next append).
    from futures_fund.content_store import _day_file, _day_key

    cd = str(tmp_path)
    first = _item(url="https://n/first", title="first", published_ts=NOW)
    store_items(cd, [first])

    # simulate a crash mid-append: a record written without its trailing newline.
    day_path = _day_file(cd, _day_key(NOW))
    with day_path.open("a") as fh:
        fh.write('{"id": "torn')  # torn, newline-less

    second = _item(url="https://n/second", title="second", published_ts=NOW)
    store_items(cd, [second])

    assert get_item(cd, first.id) is not None
    assert get_item(cd, second.id) is not None
