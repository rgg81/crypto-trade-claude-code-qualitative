from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund.content_store import (
    ContentItem,
    make_id,
    store_items,
    update_digest,
)
from futures_fund.decision_io import (
    PRICE_CARD_NOTE,
    PRICE_CARD_UNAVAILABLE_NOTE,
    RECENT_ITEMS_CAP,
    EvidencePacket,
    assemble_evidence,
    read_digest,
)

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _item(
    *,
    source: str = "coindesk",
    url: str = "https://coindesk.com/a/1",
    title: str = "Bitcoin rips higher",
    summary: str | None = None,
    coins: list[str] | None = None,
    published_ts: datetime = NOW,
    item_sentiment=None,
) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title),
        source=source,
        feed="https://coindesk.com/rss",
        url=url,
        title=title,
        body="",
        author="",
        coins=coins if coins is not None else ["BTC"],
        published_ts=published_ts,
        fetched_ts=NOW,
        engagement={},
        summary=summary,
        item_sentiment=item_sentiment,
    )


_MARKS = {"BTC": 65000.0, "ETH": 3500.0}
_ATRS = {"BTC": 1200.0, "ETH": 80.0}


def _fake_price_fn(coin: str) -> tuple[float, float]:
    return (_MARKS.get(coin, 1.0), _ATRS.get(coin, 0.5))


# --- in-window / out-of-window filtering -------------------------------------


def test_only_in_window_and_in_store_items_are_included(tmp_path: Path) -> None:
    in_window = _item(
        url="https://coindesk.com/in",
        title="BTC in window",
        summary="bullish flows",
        published_ts=NOW - timedelta(hours=10),
        item_sentiment="positive",
    )
    out_of_window = _item(
        url="https://coindesk.com/out",
        title="BTC stale",
        published_ts=NOW - timedelta(hours=200),  # > 96h window
        item_sentiment="negative",
    )
    store_items(tmp_path, [in_window, out_of_window])

    packets = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)
    pkt = packets["BTC"]
    ids = {r["item_id"] for r in pkt.recent_items}
    assert in_window.id in ids
    assert out_of_window.id not in ids
    assert len(pkt.recent_items) == 1


def test_window_boundary_is_inclusive(tmp_path: Path) -> None:
    exactly_at = _item(
        url="https://coindesk.com/edge",
        title="BTC at edge",
        published_ts=NOW - timedelta(hours=96),
    )
    store_items(tmp_path, [exactly_at])
    packets = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)
    ids = {r["item_id"] for r in packets["BTC"].recent_items}
    assert exactly_at.id in ids


def test_recent_items_carry_reduced_fields_and_are_newest_first(tmp_path: Path) -> None:
    older = _item(
        url="https://coindesk.com/older",
        title="older",
        summary="s-older",
        published_ts=NOW - timedelta(hours=50),
        item_sentiment="neutral",
    )
    newer = _item(
        url="https://coindesk.com/newer",
        title="newer",
        summary="s-newer",
        published_ts=NOW - timedelta(hours=5),
        item_sentiment="very_positive",
    )
    store_items(tmp_path, [older, newer])

    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, price_fn=_fake_price_fn)["BTC"]
    assert [r["item_id"] for r in pkt.recent_items] == [newer.id, older.id]  # newest first
    row = pkt.recent_items[0]
    assert set(row.keys()) == {
        "item_id", "title", "summary", "source", "published_ts", "item_sentiment",
        "age_hours", "age_bucket",
    }
    assert row["title"] == "newer"
    assert row["summary"] == "s-newer"
    assert row["source"] == "coindesk"
    assert row["item_sentiment"] == "very_positive"


def test_source_breakdown_counts_recent_items(tmp_path: Path) -> None:
    h = timedelta(hours=1)
    store_items(tmp_path, [
        _item(source="coindesk", url="https://x/1", title="a", published_ts=NOW - 1 * h),
        _item(source="coindesk", url="https://x/2", title="b", published_ts=NOW - 2 * h),
        _item(source="theblock", url="https://x/3", title="c", published_ts=NOW - 3 * h),
        # out of window — must NOT count toward the breakdown
        _item(source="reddit", url="https://x/4", title="d", published_ts=NOW - 300 * h),
    ])
    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)["BTC"]
    assert pkt.source_breakdown == {"coindesk": 2, "theblock": 1}


# --- price card (RISK PLUMBING ONLY) -----------------------------------------


def test_price_card_carries_non_directional_note(tmp_path: Path) -> None:
    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, price_fn=_fake_price_fn)["BTC"]
    assert pkt.price_card is not None
    assert pkt.price_card["mark"] == 65000.0
    assert pkt.price_card["atr"] == 1200.0
    assert pkt.price_card["note"] == PRICE_CARD_NOTE
    assert "NON-DIRECTIONAL" in pkt.price_card["note"]
    assert "not a trading signal" in pkt.price_card["note"]


def test_missing_price_fn_degrades_gracefully(tmp_path: Path) -> None:
    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, price_fn=None)["BTC"]
    # the packet is still assembled; only the (non-directional) plumbing is missing
    assert pkt.price_card is not None
    assert pkt.price_card["mark"] is None
    assert pkt.price_card["atr"] is None
    assert pkt.price_card["note"] == PRICE_CARD_UNAVAILABLE_NOTE


def test_failing_price_fn_is_fail_soft(tmp_path: Path) -> None:
    def boom(coin: str):
        raise RuntimeError("exchange down")

    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, price_fn=boom)["BTC"]
    assert pkt.price_card["mark"] is None
    assert pkt.price_card["note"] == PRICE_CARD_UNAVAILABLE_NOTE


def test_malformed_price_fn_return_is_fail_soft(tmp_path: Path) -> None:
    def bad(coin: str):
        return ("not-a-number", None)

    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, price_fn=bad)["BTC"]
    assert pkt.price_card["mark"] is None
    assert pkt.price_card["note"] == PRICE_CARD_UNAVAILABLE_NOTE


# --- digest ------------------------------------------------------------------


def test_digest_is_attached_when_present(tmp_path: Path) -> None:
    store_items(tmp_path, [
        _item(published_ts=NOW - timedelta(hours=3), item_sentiment="positive"),
    ])
    update_digest(tmp_path, "BTC", NOW)
    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, price_fn=_fake_price_fn)["BTC"]
    assert pkt.digest != {}
    assert pkt.digest["coin"] == "BTC"
    assert pkt.digest["n_items_30d"] == 1


def test_missing_digest_degrades_to_empty_dict(tmp_path: Path) -> None:
    assert read_digest(tmp_path, "DOGE") == {}
    pkt = assemble_evidence(tmp_path, ["DOGE"], NOW, price_fn=_fake_price_fn)["DOGE"]
    assert pkt.digest == {}


def test_torn_digest_file_degrades_to_empty_dict(tmp_path: Path) -> None:
    digest_path = tmp_path / "digests" / "BTC.json"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text("{ this is not valid json")
    assert read_digest(tmp_path, "BTC") == {}


# --- packet shape / multi-coin -----------------------------------------------


def test_assemble_keyed_by_upper_coin_and_returns_packets(tmp_path: Path) -> None:
    store_items(tmp_path, [
        _item(coins=["BTC"], url="https://x/btc", title="btc news",
              published_ts=NOW - timedelta(hours=1)),
        _item(coins=["ETH"], source="theblock", url="https://x/eth", title="eth news",
              published_ts=NOW - timedelta(hours=1)),
    ])
    packets = assemble_evidence(tmp_path, ["btc", "eth"], NOW, price_fn=_fake_price_fn)
    assert set(packets.keys()) == {"BTC", "ETH"}
    assert isinstance(packets["BTC"], EvidencePacket)
    assert packets["BTC"].as_of_ts == NOW
    assert packets["ETH"].price_card["mark"] == 3500.0


def test_coin_with_no_items_yields_empty_recent_items(tmp_path: Path) -> None:
    pkt = assemble_evidence(tmp_path, ["SOL"], NOW, price_fn=_fake_price_fn)["SOL"]
    assert pkt.recent_items == []
    assert pkt.source_breakdown == {}
    assert pkt.digest == {}
    # price card still present (risk plumbing is coin-independent)
    assert pkt.price_card["note"] == PRICE_CARD_NOTE


def test_naive_now_is_treated_as_utc(tmp_path: Path) -> None:
    naive_now = datetime(2026, 6, 13, 12, 0, 0)  # no tzinfo
    store_items(tmp_path, [_item(published_ts=NOW - timedelta(hours=2))])
    pkt = assemble_evidence(tmp_path, ["BTC"], naive_now, price_fn=_fake_price_fn)["BTC"]
    assert pkt.as_of_ts.tzinfo is not None
    assert len(pkt.recent_items) == 1


# --- read-layer recency: truncation / age fields / freshness -----------------


def test_recent_items_truncated_to_cap_keeping_the_newest(tmp_path: Path) -> None:
    # RECENT_ITEMS_CAP + 10 in-window items, all distinct, spaced 1h apart inside the 96h window.
    n = RECENT_ITEMS_CAP + 10
    items = [
        _item(
            url=f"https://coindesk.com/i/{i}",
            title=f"BTC item {i}",
            # i=0 is the OLDEST (n-1 hours back), i=n-1 is the NEWEST (1 hour back)
            published_ts=NOW - timedelta(hours=(n - i)),
        )
        for i in range(n)
    ]
    store_items(tmp_path, items)

    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)["BTC"]
    # truncated to the cap...
    assert len(pkt.recent_items) == RECENT_ITEMS_CAP
    # ...keeping the NEWEST cap items (the last `cap` of the spaced series), newest-first.
    expected_newest = [it.id for it in items[-RECENT_ITEMS_CAP:]][::-1]
    assert [r["item_id"] for r in pkt.recent_items] == expected_newest
    # the dropped (older) ones are absent
    dropped = {it.id for it in items[:-RECENT_ITEMS_CAP]}
    assert dropped.isdisjoint({r["item_id"] for r in pkt.recent_items})


def test_age_hours_and_bucket_are_correct_vs_as_of_ts(tmp_path: Path) -> None:
    fresh = _item(url="https://x/fresh", title="fresh", published_ts=NOW - timedelta(hours=10))
    edge24 = _item(url="https://x/e24", title="e24", published_ts=NOW - timedelta(hours=24))
    recent = _item(url="https://x/recent", title="recent", published_ts=NOW - timedelta(hours=40))
    edge48 = _item(url="https://x/e48", title="e48", published_ts=NOW - timedelta(hours=48))
    stale = _item(url="https://x/stale", title="stale", published_ts=NOW - timedelta(hours=72))
    store_items(tmp_path, [fresh, edge24, recent, edge48, stale])

    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)["BTC"]
    by_id = {r["item_id"]: r for r in pkt.recent_items}

    assert by_id[fresh.id]["age_hours"] == 10.0
    assert by_id[fresh.id]["age_bucket"] == "fresh<=24h"
    # boundary 24h is inclusive of "fresh"
    assert by_id[edge24.id]["age_hours"] == 24.0
    assert by_id[edge24.id]["age_bucket"] == "fresh<=24h"
    assert by_id[recent.id]["age_hours"] == 40.0
    assert by_id[recent.id]["age_bucket"] == "recent<=48h"
    # boundary 48h is inclusive of "recent"
    assert by_id[edge48.id]["age_hours"] == 48.0
    assert by_id[edge48.id]["age_bucket"] == "recent<=48h"
    assert by_id[stale.id]["age_hours"] == 72.0
    assert by_id[stale.id]["age_bucket"] == "stale>48h"


def test_age_hours_floored_at_zero_for_future_dated_item(tmp_path: Path) -> None:
    # a (slightly) future-dated item — source clock skew — reads as age 0.0, not negative.
    skewed = _item(url="https://x/skew", title="skew", published_ts=NOW + timedelta(hours=2))
    store_items(tmp_path, [skewed])
    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)["BTC"]
    row = next(r for r in pkt.recent_items if r["item_id"] == skewed.id)
    assert row["age_hours"] == 0.0
    assert row["age_bucket"] == "fresh<=24h"


def test_freshness_is_fraction_of_items_within_24h(tmp_path: Path) -> None:
    # 3 of 4 in-window items are <=24h old -> freshness 0.75
    store_items(tmp_path, [
        _item(url="https://x/1", title="a", published_ts=NOW - timedelta(hours=1)),
        _item(url="https://x/2", title="b", published_ts=NOW - timedelta(hours=12)),
        _item(url="https://x/3", title="c", published_ts=NOW - timedelta(hours=24)),  # boundary
        _item(url="https://x/4", title="d", published_ts=NOW - timedelta(hours=60)),  # stale
    ])
    pkt = assemble_evidence(tmp_path, ["BTC"], NOW, window_hours=96, price_fn=_fake_price_fn)["BTC"]
    assert len(pkt.recent_items) == 4
    assert pkt.freshness == 0.75


def test_freshness_is_zero_when_no_recent_items(tmp_path: Path) -> None:
    pkt = assemble_evidence(tmp_path, ["SOL"], NOW, price_fn=_fake_price_fn)["SOL"]
    assert pkt.recent_items == []
    assert pkt.freshness == 0.0
