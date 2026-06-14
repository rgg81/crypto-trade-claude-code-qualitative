"""Offline tests for the 15-minute crawl tick (futures_fund/crawler).

No live network and no real adapters: FAKE :class:`SourceAdapter`s (one of which RAISES) are run
against a FAKE client over tmp content/state dirs with an injected ``now``. We assert the tick is
source-isolated (a raising adapter is recorded as an error + a health failure but never aborts the
tick), only NEW (deduped) items are counted, the digest + purge bookkeeping is invoked, and after K
consecutive failures a source is circuit-broken and SKIPPED on the next tick (-> ``degraded``).
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from futures_fund import content_store, crawler, source_health
from futures_fund.content_store import ContentItem, make_id
from futures_fund.sources.base import SourceAdapter

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)
UNIVERSE = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]


# --------------------------------------------------------------------------- #
# fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeClient:
    """Inert httpx-like client — the fake adapters ignore it; it only proves it is threaded in."""

    def __init__(self) -> None:
        self.calls: list = []

    def get(self, *a, **k):  # pragma: no cover - never hit by the fakes
        self.calls.append((a, k))
        return None


class ClientDependentAdapter(SourceAdapter):
    """Behaves like a real adapter: with a real client it yields items, with ``client is None`` it
    is a silent no-op returning []. Used to prove the tick never stamps a source healthy on a no-op
    tick driven by a None client."""

    def __init__(self, name: str, items: list[ContentItem]) -> None:
        super().__init__()
        self.name = name
        self._items = items
        self.calls = 0
        self.last_client = "unset"

    def fetch(self, client, cfg, universe):
        self.calls += 1
        self.last_client = client
        if client is None:
            return []
        return list(self._items)


def _item(source: str, *, url: str, title: str, coins: list[str], published=None) -> ContentItem:
    pub = published or NOW
    return ContentItem(
        id=make_id(source, url, title),
        source=source,
        feed=f"{source}-feed",
        url=url,
        title=title,
        body="",
        author="",
        published_ts=pub,
        fetched_ts=NOW,
        coins=coins,
    )


class StaticAdapter(SourceAdapter):
    """Returns a fixed list of items and records that fetch ran with the injected client."""

    def __init__(self, name: str, items: list[ContentItem]) -> None:
        super().__init__()
        self.name = name
        self._items = items
        self.calls = 0
        self.last_client = None

    def fetch(self, client, cfg, universe):
        self.calls += 1
        self.last_client = client
        return list(self._items)


class RaisingAdapter(SourceAdapter):
    """A misbehaving adapter that RAISES inside fetch — must be isolated, never abort the tick."""

    def __init__(self, name: str = "boom") -> None:
        super().__init__()
        self.name = name
        self.calls = 0

    def fetch(self, client, cfg, universe):
        self.calls += 1
        raise RuntimeError("adapter exploded")


class HangingAdapter(SourceAdapter):
    """A misbehaving adapter that HANGS inside fetch — must be bounded by the tick deadline, never
    block the tick forever. (No network: a plain non-cancellable busy/sleep loop.)"""

    def __init__(self, name: str = "hang", sleep_s: float = 3600.0) -> None:
        super().__init__()
        self.name = name
        self.sleep_s = sleep_s
        self.calls = 0

    def fetch(self, client, cfg, universe):
        self.calls += 1
        time.sleep(self.sleep_s)
        return []


def _registry(*adapters: SourceAdapter) -> dict[str, SourceAdapter]:
    return {a.name: a for a in adapters}


def _cfg(*names: str) -> dict:
    return {"sources": list(names)}


# --------------------------------------------------------------------------- #
# tick basics                                                                  #
# --------------------------------------------------------------------------- #


def test_tick_stores_items_and_threads_client(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    client = FakeClient()

    rss = StaticAdapter("rss", [_item("rss", url="u/1", title="Bitcoin up", coins=["BTC"])])
    reddit = StaticAdapter(
        "reddit", [_item("reddit", url="u/2", title="ETH moon", coins=["ETH"])]
    )
    reg = _registry(rss, reddit)

    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss", "reddit"), UNIVERSE, NOW, client=client, registry=reg
    )

    assert isinstance(res, crawler.CrawlResult)
    assert res.new_items == 2
    assert res.per_source == {"rss": 1, "reddit": 1}
    assert res.errors == {}
    assert res.degraded == []
    # the injected client was threaded through to each adapter
    assert rss.last_client is client and reddit.last_client is client
    # items actually landed in the store
    assert content_store.get_item(content_dir, make_id("rss", "u/1", "Bitcoin up")) is not None


def test_raising_adapter_isolated_does_not_break_tick(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"

    good = StaticAdapter("rss", [_item("rss", url="u/1", title="BTC news", coins=["BTC"])])
    bad = RaisingAdapter("boom")
    reg = _registry(good, bad)

    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss", "boom"), UNIVERSE, NOW, client=FakeClient(), registry=reg
    )

    # the good source still produced its item
    assert res.new_items == 1
    assert res.per_source.get("rss") == 1
    # the bad source is recorded as an error (not raised) and counted as zero
    assert "boom" in res.errors
    assert "adapter exploded" in res.errors["boom"]
    assert res.per_source.get("boom") == 0

    # and its failure is persisted to source_health
    health = source_health.load_health(state_dir)
    assert health.sources["boom"].consecutive_failures == 1
    assert health.sources["boom"].total_err == 1
    # the good source recorded a success with a latency
    assert health.sources["rss"].total_ok == 1
    assert health.sources["rss"].last_latency_ms is not None


def test_only_new_deduped_items_counted_across_ticks(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"

    # batch 1: two distinct items
    a = _item("rss", url="u/1", title="BTC one", coins=["BTC"])
    b = _item("rss", url="u/2", title="ETH two", coins=["ETH"])
    reg1 = _registry(StaticAdapter("rss", [a, b]))
    res1 = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss"), UNIVERSE, NOW, client=FakeClient(), registry=reg1
    )
    assert res1.new_items == 2

    # batch 2: 'a' again (dup), plus a genuinely new 'c'. Only 'c' is new.
    c = _item("rss", url="u/3", title="SOL three", coins=["SOL"])
    reg2 = _registry(StaticAdapter("rss", [a, c]))
    res2 = crawler.crawl_tick(
        content_dir,
        state_dir,
        _cfg("rss"),
        UNIVERSE,
        NOW + timedelta(minutes=15),
        client=FakeClient(),
        registry=reg2,
    )
    assert res2.per_source == {"rss": 2}  # raw count returned by the adapter
    assert res2.new_items == 1  # only 'c' deduped through


def test_in_batch_duplicate_counted_once(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    # two adapters emit the SAME ContentItem id in one tick -> each counts as 1 returned, but the
    # content store dedupes within the batch so only ONE item is actually stored / counted new.
    same_id = _item("rss", url="u/x", title="Same headline", coins=["ETH"])
    reg = _registry(StaticAdapter("rss", [same_id]), StaticAdapter("reddit", [same_id]))
    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss", "reddit"), UNIVERSE, NOW, client=FakeClient(), registry=reg
    )
    assert res.per_source == {"rss": 1, "reddit": 1}  # each adapter returned 1
    assert res.new_items == 1  # but it deduped to one stored item


def test_hanging_adapter_is_bounded_by_tick_deadline(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"

    good = StaticAdapter("rss", [_item("rss", url="u/1", title="BTC news", coins=["BTC"])])
    slow = HangingAdapter("hang", sleep_s=3600.0)
    reg = _registry(good, slow)

    t0 = time.monotonic()
    res = crawler.crawl_tick(
        content_dir,
        state_dir,
        _cfg("rss", "hang"),
        UNIVERSE,
        NOW,
        client=FakeClient(),
        registry=reg,
        tick_budget_s=1.0,
    )
    elapsed = time.monotonic() - t0

    # the tick must NOT block on the hung worker — it returns within a small bound of the budget.
    assert elapsed < 10.0, f"crawl_tick took {elapsed:.1f}s — it blocked on the hung adapter"
    # the good source still produced its item
    assert res.per_source.get("rss") == 1
    assert content_store.get_item(content_dir, make_id("rss", "u/1", "BTC news")) is not None
    # the hung source is recorded as an error + a health failure, not silently dropped
    assert "hang" in res.errors
    health = source_health.load_health(state_dir)
    assert health.sources["hang"].consecutive_failures == 1
    assert health.sources["hang"].total_err == 1


# --------------------------------------------------------------------------- #
# bookkeeping: digests + purge invoked                                         #
# --------------------------------------------------------------------------- #


def test_digests_updated_for_touched_coins_and_purge_invoked(tmp_path, monkeypatch):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"

    digest_calls: list[str] = []
    purge_calls: list[datetime] = []

    real_update = content_store.update_digest
    real_purge = content_store.purge

    def spy_update(cdir, coin, when, *a, **k):
        digest_calls.append(coin)
        return real_update(cdir, coin, when, *a, **k)

    def spy_purge(cdir, when, *a, **k):
        purge_calls.append(when)
        return real_purge(cdir, when, *a, **k)

    monkeypatch.setattr(crawler.content_store, "update_digest", spy_update)
    monkeypatch.setattr(crawler.content_store, "purge", spy_purge)

    rss = StaticAdapter(
        "rss",
        [
            _item("rss", url="u/1", title="BTC and ETH rally", coins=["BTC", "ETH"]),
            _item("rss", url="u/2", title="ETH again", coins=["ETH"]),
        ],
    )
    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss"), UNIVERSE, NOW, client=FakeClient(), registry=_registry(rss)
    )
    assert res.new_items == 2

    # digest updated once per UNIQUE touched coin (no duplicate ETH update)
    assert sorted(digest_calls) == ["BTC", "ETH"]
    # purge invoked exactly once with the tick's now
    assert purge_calls == [NOW]
    # the digest files actually exist on disk
    assert (content_dir / "digests" / "BTC.json").exists()
    assert (content_dir / "digests" / "ETH.json").exists()


def test_no_items_no_digest_but_purge_still_runs(tmp_path, monkeypatch):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    digest_calls: list[str] = []
    purge_calls: list[datetime] = []
    monkeypatch.setattr(
        crawler.content_store, "update_digest",
        lambda *a, **k: digest_calls.append(a[1]),
    )
    monkeypatch.setattr(
        crawler.content_store, "purge", lambda *a, **k: purge_calls.append(a[1]) or {},
    )
    empty = StaticAdapter("rss", [])
    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss"), UNIVERSE, NOW, client=FakeClient(), registry=_registry(empty)
    )
    assert res.new_items == 0
    assert digest_calls == []          # nothing touched -> no digest work
    assert purge_calls == [NOW]        # purge always runs


# --------------------------------------------------------------------------- #
# circuit breaker across ticks                                                 #
# --------------------------------------------------------------------------- #


def test_circuit_breaks_after_k_failures_and_skips_next_tick(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    good = StaticAdapter("rss", [_item("rss", url="u/1", title="BTC", coins=["BTC"])])
    bad = RaisingAdapter("boom")
    reg = _registry(good, bad)
    cfg = _cfg("rss", "boom")

    # default k_threshold is 3 -> three failing ticks trip the breaker
    for i in range(3):
        t = NOW + timedelta(minutes=15 * i)
        res = crawler.crawl_tick(
            content_dir, state_dir, cfg, UNIVERSE, t, client=FakeClient(), registry=reg
        )
        assert "boom" in res.errors
        # not yet skipped on the ticks that DO the failing
        assert "boom" not in res.degraded

    bad_calls_after_trip = bad.calls
    health = source_health.load_health(state_dir)
    assert health.sources["boom"].consecutive_failures == 3
    assert source_health.is_healthy(health, "boom", NOW + timedelta(minutes=30)) is False

    # next tick (still inside the backoff window): boom is SKIPPED -> degraded, fetch NOT called.
    # 3rd failure stamped at NOW+30m -> disabled_until = NOW+45m; pick a now strictly before that.
    t_next = NOW + timedelta(minutes=40)
    res = crawler.crawl_tick(
        content_dir, state_dir, cfg, UNIVERSE, t_next, client=FakeClient(), registry=reg
    )
    assert "boom" in res.degraded
    assert "boom" not in res.errors          # not run -> not an error this tick
    assert "boom" not in res.per_source       # skipped entirely
    assert bad.calls == bad_calls_after_trip  # fetch was not invoked again
    # the good source keeps running while boom is parked
    assert res.per_source.get("rss") == 1


def test_recovers_after_backoff_window_elapses(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    bad = RaisingAdapter("boom")
    reg = _registry(bad)
    cfg = _cfg("boom")

    for i in range(3):
        crawler.crawl_tick(
            content_dir, state_dir, cfg, UNIVERSE, NOW + timedelta(minutes=15 * i),
            client=FakeClient(), registry=reg,
        )
    # 3rd trip stamped at NOW+30m -> disabled_until = NOW+45m (base_backoff 15m, 2**0)
    skip_calls = bad.calls

    # still within backoff -> skipped
    res_skip = crawler.crawl_tick(
        content_dir, state_dir, cfg, UNIVERSE, NOW + timedelta(minutes=40),
        client=FakeClient(), registry=reg,
    )
    assert res_skip.degraded == ["boom"]
    assert bad.calls == skip_calls

    # past the breaker -> retried (and fails again, re-tripping)
    res_retry = crawler.crawl_tick(
        content_dir, state_dir, cfg, UNIVERSE, NOW + timedelta(minutes=60),
        client=FakeClient(), registry=reg,
    )
    assert "boom" not in res_retry.degraded
    assert "boom" in res_retry.errors
    assert bad.calls == skip_calls + 1


def test_single_success_recovers_health(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    # Seed health with a tripped breaker for 'rss' that has already EXPIRED at NOW.
    health = source_health.SourceHealth()
    for _ in range(3):
        source_health.record_err(health, "rss", now=NOW - timedelta(hours=1))
    source_health.save_health(state_dir, health)

    good = StaticAdapter("rss", [_item("rss", url="u/1", title="BTC back", coins=["BTC"])])
    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss"), UNIVERSE, NOW, client=FakeClient(),
        registry=_registry(good),
    )
    assert res.new_items == 1
    assert "rss" not in res.degraded
    reloaded = source_health.load_health(state_dir)
    assert reloaded.sources["rss"].consecutive_failures == 0
    assert reloaded.sources["rss"].disabled_until is None
    assert reloaded.sources["rss"].total_ok == 1


# --------------------------------------------------------------------------- #
# enabled-block filtering + per-source config                                  #
# --------------------------------------------------------------------------- #


def test_only_enabled_sources_run(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    a = StaticAdapter("rss", [_item("rss", url="u/1", title="BTC", coins=["BTC"])])
    b = StaticAdapter("reddit", [_item("reddit", url="u/2", title="ETH", coins=["ETH"])])
    reg = _registry(a, b)
    # only enable rss
    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss"), UNIVERSE, NOW, client=FakeClient(), registry=reg
    )
    assert res.per_source == {"rss": 1}
    assert a.calls == 1 and b.calls == 0


def test_per_source_config_block_passed_through(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    seen: dict = {}

    class CfgCapturingAdapter(SourceAdapter):
        name = "rss"

        def fetch(self, client, cfg, universe):
            seen["cfg"] = cfg
            return []

    cfg = {"sources": ["rss"], "sources_config": {"rss": {"feeds": ["http://x"]}}}
    crawler.crawl_tick(
        content_dir, state_dir, cfg, UNIVERSE, NOW, client=FakeClient(),
        registry=_registry(CfgCapturingAdapter()),
    )
    assert seen["cfg"] == {"feeds": ["http://x"]}


def test_none_client_does_not_stamp_sources_healthy(tmp_path):
    # A None client makes every adapter a silent no-op. The tick MUST NOT record_ok (stamp the
    # source healthy) on a no-op: a None client is a hard error condition, recorded as such.
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"

    a = ClientDependentAdapter("rss", [_item("rss", url="u/1", title="BTC", coins=["BTC"])])
    reg = _registry(a)

    res = crawler.crawl_tick(
        content_dir, state_dir, _cfg("rss"), UNIVERSE, NOW, client=None, registry=reg
    )

    # no items produced (no-op), and the source is recorded as an ERROR — never healthy.
    assert res.new_items == 0
    assert "rss" in res.errors
    assert res.per_source.get("rss") == 0

    health = source_health.load_health(state_dir)
    assert health.sources["rss"].total_ok == 0
    assert health.sources["rss"].total_err == 1
    assert health.sources["rss"].consecutive_failures == 1
    # the adapter was NOT even invoked with a None client (we short-circuit before the no-op fetch)
    assert a.calls == 0


def test_no_enabled_sources_is_a_clean_empty_tick(tmp_path):
    content_dir = tmp_path / "content"
    state_dir = tmp_path / "state"
    res = crawler.crawl_tick(
        content_dir, state_dir, {"sources": ["nope"]}, UNIVERSE, NOW,
        client=FakeClient(), registry=_registry(StaticAdapter("rss", [])),
    )
    assert res.new_items == 0
    assert res.per_source == {}
    assert res.errors == {}
    assert res.degraded == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
