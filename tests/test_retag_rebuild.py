"""Offline tests for scripts/retag_rebuild.py — the one-shot pass that re-runs the FIXED intake
(word-boundary tagging + canonical cross-source dedup) over the existing on-disk content store and
rebuilds the per-coin index + digests. No network; a crafted tmp store is written by hand."""
from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

from futures_fund.content_store import (
    ContentItem,
    _as_utc,
    _read_jsonl,
    index_for_coin,
    make_id,
)

# import the script module by path (scripts/ is not a package on sys.path in tests)
_SPEC = importlib.util.spec_from_file_location(
    "retag_rebuild", Path(__file__).resolve().parent.parent / "scripts" / "retag_rebuild.py"
)
retag_rebuild = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(retag_rebuild)

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _item(*, source, url, title, body="", coins, item_sentiment=None, summary=None,
          published_ts=NOW, fetched_ts=NOW) -> ContentItem:
    return ContentItem(
        id=make_id(source, url, title),
        source=source,
        feed="feed",
        url=url,
        title=title,
        body=body,
        coins=coins,
        published_ts=published_ts,
        fetched_ts=fetched_ts,
        item_sentiment=item_sentiment,
        summary=summary,
    )


def _write_store(content_dir: str, items: list[ContentItem]) -> None:
    """Write items straight into the day files WITHOUT going through the (now-fixed) store_items, so
    the crafted store can carry the OLD corrupt state (false tags + un-collapsed cross-source dups)
    that the rebuild has to repair. Partitioned by published-day, like the real day-file layout."""
    items_dir = Path(content_dir) / "items"
    items_dir.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[str]] = {}
    for it in items:
        day = _as_utc(it.published_ts).date().isoformat()
        by_day.setdefault(day, []).append(json.loads(it.model_dump_json()))
    for day, rows in by_day.items():
        text = "".join(json.dumps(r, default=str) + "\n" for r in rows)
        (items_dir / f"items-{day}.jsonl").write_text(text)


def test_false_tagged_link_item_gets_untagged(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # An item whose ONLY "LINK" came from the substring "linked" — the old substring tagger tagged
    # it LINK; the fixed word-boundary tagger must NOT. (BTC is a real whole-token mention.)
    false_link = _item(
        source="reddit",
        url="https://r/iran",
        title="Iran-Linked Group Threatens Bitcoin Exchanges",
        body="A hacking crew said to threaten exchanges.",
        coins=["LINK", "BTC"],  # corrupt: LINK is a false substring tag
        item_sentiment="negative",
        summary="threat to exchanges",
    )
    # a genuine Chainlink mention via the $LINK cashtag must SURVIVE the re-tag
    real_link = _item(
        source="stocktwits",
        url="https://st/link1",
        title="$LINK.X breaking out hard",
        body="$LINK.X grinding up",
        coins=["LINK"],
        item_sentiment="positive",
    )
    _write_store(cd, [false_link, real_link])

    out = retag_rebuild.rebuild(cd, config_path="config.yaml", now_arg="2026-06-13T12:00:00Z")

    # the false LINK pointer is gone; the genuine one remains
    link_ptrs = index_for_coin(cd, "LINK", NOW.replace(hour=0))
    assert {p["item_id"] for p in link_ptrs} == {real_link.id}
    # BTC tag on the re-read item survives (it was a real whole-token mention)
    btc_ptrs = index_for_coin(cd, "BTC", NOW.replace(hour=0))
    assert false_link.id in {p["item_id"] for p in btc_ptrs}
    # the LINK digest reflects exactly one item now
    assert out["after"]["LINK"]["n_items_30d"] == 1


def test_cross_source_dup_collapses_keeping_richest_and_verdict(tmp_path: Path) -> None:
    cd = str(tmp_path)
    # SAME article under two source names sharing a canonical url. The richer (longer-body) copy is
    # kept; an item_sentiment carried only by the SHORTER copy must be preserved on the survivor.
    rss = _item(
        source="rss",
        url="https://news.com/etf?utm_source=tw",
        title="Spot Bitcoin ETF Approved",
        body="short stub",
        coins=["BTC"],
        item_sentiment="positive",   # the verdict lives on the SHORTER copy
        summary="etf approved, bullish",
    )
    forums = _item(
        source="forums",
        url="https://news.com/etf",
        title="Spot Bitcoin ETF Approved",
        body="a much longer and richer article body with far more detail than the rss stub",
        coins=["BTC"],
        item_sentiment=None,         # the richer copy was never summarized
    )
    _write_store(cd, [rss, forums])
    # the crafted store really does hold BOTH copies before the rebuild
    rows = _read_jsonl(Path(cd) / "items" / "items-2026-06-13.jsonl")
    assert len(rows) == 2

    out = retag_rebuild.rebuild(cd, config_path="config.yaml", now_arg="2026-06-13T12:00:00Z")
    assert out["items_before"] == 2
    assert out["items_after"] == 1
    assert out["collapsed"] == 1

    rows = _read_jsonl(Path(cd) / "items" / "items-2026-06-13.jsonl")
    assert len(rows) == 1
    kept = rows[0]
    # richest body survived AND inherited the verdict from the dropped (shorter) copy
    assert kept["body"].startswith("a much longer and richer")
    assert kept["item_sentiment"] == "positive"
    assert kept["summary"] == "etf approved, bullish"
    # exactly one BTC pointer + one item in the digest
    btc_ptrs = index_for_coin(cd, "BTC", NOW.replace(hour=0))
    assert len(btc_ptrs) == 1
    assert out["after"]["BTC"]["n_items_30d"] == 1


def test_rebuild_is_idempotent(tmp_path: Path) -> None:
    cd = str(tmp_path)
    _write_store(cd, [
        _item(source="reddit", url="https://r/a", title="Bitcoin linked rally",
              body="blockchain", coins=["BTC", "LINK"]),
        _item(source="rss", url="https://news.com/x?utm=1", title="ETH update",
              body="ethereum upgrade", coins=["ETH"]),
        _item(source="forums", url="https://news.com/x", title="ETH update",
              body="ethereum upgrade richer body", coins=["ETH"]),
    ])
    day = Path(cd) / "items" / "items-2026-06-13.jsonl"

    first = retag_rebuild.rebuild(cd, now_arg="2026-06-13T12:00:00Z")
    after_first = day.read_text()
    idx_first = (Path(cd) / "index" / "by_coin" / "ETH.jsonl").read_text()

    second = retag_rebuild.rebuild(cd, now_arg="2026-06-13T12:00:00Z")
    # a second pass over the already-clean store changes nothing on disk
    assert day.read_text() == after_first
    assert (Path(cd) / "index" / "by_coin" / "ETH.jsonl").read_text() == idx_first
    assert second["items_after"] == first["items_after"]
    assert second["collapsed"] == 0  # nothing left to collapse the second time


def test_now_defaults_to_newest_fetched_ts(tmp_path: Path) -> None:
    cd = str(tmp_path)
    newest = datetime(2026, 6, 13, 20, 30, 0, tzinfo=UTC)
    _write_store(cd, [
        _item(source="rss", url="https://n/1", title="Bitcoin one", coins=["BTC"],
              fetched_ts=datetime(2026, 6, 10, 1, 0, 0, tzinfo=UTC)),
        _item(source="rss", url="https://n/2", title="Bitcoin two", coins=["BTC"],
              fetched_ts=newest),
    ])
    out = retag_rebuild.rebuild(cd)  # no --now
    assert out["now"] == newest.isoformat()
