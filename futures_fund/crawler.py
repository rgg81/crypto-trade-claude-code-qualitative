"""One 15-minute crawl tick — the deterministic ingest heartbeat of the qualitative desk.

A *tick* fans out the enabled, currently-healthy source adapters over a bounded thread pool, pulls
their :class:`~futures_fund.content_store.ContentItem`s, dedupes + persists them into the 30-day
:mod:`~futures_fund.content_store`, refreshes the per-coin decayed-sentiment digests for every coin
that was touched, purges anything past the retention window, and finally persists the rolling
:class:`~futures_fund.source_health.SourceHealth` (latency / failure streaks / circuit breakers).

Hard rules (mirroring base ``futures_fund/vendors.py`` + the adapter contract):

* **Fail-soft, source-isolated** — an adapter that RAISES or hangs only loses *its own* slot: the
  failure is recorded against its health (``record_err`` -> eventual circuit-break) and the tick
  continues with whatever the other sources returned. One bad upstream never aborts the tick.
* **Circuit-broken sources are skipped** — only adapters that :func:`source_health.is_healthy`
  reports as eligible *this* ``now`` are run; the rest land in :attr:`CrawlResult.degraded`.
* **Injected everything** — ``now`` (the tick clock) and ``client`` (an httpx-like client, real or
  fake) are injected, and all I/O goes through the content-store/health atomic writers, so a tick
  is deterministic and trivially testable offline.

The LLM summarisation of stored items is a SEPARATE downstream step — a tick is purely mechanical
fetch + store + bookkeeping and never calls a model.
"""
from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, Field

from futures_fund import content_store, source_health
from futures_fund.content_store import ContentItem
from futures_fund.sources import SOURCE_ADAPTERS, enabled_adapters
from futures_fund.sources.base import SourceAdapter, cfg_get


class CrawlResult(BaseModel):
    """The outcome of one crawl tick.

    * ``new_items``   — count of items newly stored this tick (post-dedupe across the whole store).
    * ``per_source``  — ``{source_name: items_returned}`` for each adapter that actually RAN
                        (the raw count it returned, before cross-source dedupe).
    * ``errors``      — ``{source_name: error_repr}`` for each adapter that raised this tick.
    * ``degraded``    — sources that were SKIPPED this tick because they are circuit-broken.
    """

    new_items: int = 0
    per_source: dict[str, int] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    degraded: list[str] = Field(default_factory=list)


def _source_cfg(cfg, name: str):
    """Per-source config block for adapter ``name``, if the config carries one.

    Looks for a ``sources_config`` mapping keyed by source name (``cfg.sources_config[name]``);
    when absent we hand the adapter the whole ``cfg`` — adapters read their keys defensively via
    :func:`cfg_get`, so either shape works. Read fail-soft: any odd shape falls back to ``cfg``.
    """
    block = cfg_get(cfg, "sources_config", None)
    if isinstance(block, dict):
        sub = block.get(name)
        if sub is not None:
            return sub
    return cfg


def _run_adapter(
    adapter: SourceAdapter,
    client,
    cfg,
    universe: list[str],
) -> tuple[list[ContentItem], float]:
    """Run ONE adapter and time it. Returns ``(items, elapsed_ms)``.

    Adapters are contractually fail-soft, but we still run inside the pool isolated per-future so a
    misbehaving adapter that DOES raise (or hangs until the pool drains) is caught by the caller and
    recorded as an error rather than aborting the tick.
    """
    t0 = time.monotonic()
    items = adapter.fetch(client, _source_cfg(cfg, adapter.name), universe)
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    if not isinstance(items, list):
        items = []
    return items, elapsed_ms


# An outcome is (items, elapsed_ms, error_repr): a SUCCESS has items + elapsed and err=None; a
# FAILURE (the adapter raised) has items=None + the error string; a source that never reports back
# before the deadline (a HANG) is simply ABSENT from the results map -> treated as a timeout.
def _fan_out(
    adapters: list[SourceAdapter],
    client,
    cfg,
    universe: list[str],
    max_workers: int,
    tick_budget_s: float,
) -> dict[str, tuple]:
    """Run ``adapters`` over bounded DAEMON worker threads and collect outcomes by source name.

    Bounded by a wall-clock ``tick_budget_s`` deadline: whatever has reported back to the result
    queue by the deadline is returned; an adapter still running (a HANG, or a client that ignores
    its own timeout, or a non-network busy loop) is simply LEFT OUT of the map — the caller treats
    its absence as a timeout error + health failure. The workers are DAEMON threads, so an
    abandoned hung worker can never pin this tick AND can never block interpreter shutdown; it is
    reclaimed when the process exits. One bad upstream never aborts the tick.
    """
    work: queue.Queue[SourceAdapter] = queue.Queue()
    for adapter in adapters:
        work.put(adapter)
    out_q: queue.Queue[tuple[str, tuple]] = queue.Queue()
    n_workers = max(1, min(int(max_workers), len(adapters)))

    def _worker() -> None:
        while True:
            try:
                adapter = work.get_nowait()
            except queue.Empty:
                return
            try:
                items, elapsed_ms = _run_adapter(adapter, client, cfg, universe)
                out_q.put((adapter.name, (items, elapsed_ms, None)))
            except Exception as exc:  # noqa: BLE001 — isolate a raising adapter
                out_q.put((adapter.name, (None, 0.0, repr(exc))))

    threads = [
        threading.Thread(target=_worker, name=f"crawl-{i}", daemon=True)
        for i in range(n_workers)
    ]
    for t in threads:
        t.start()

    results: dict[str, tuple] = {}
    deadline = time.monotonic() + max(0.0, float(tick_budget_s))
    expected = len(adapters)
    while len(results) < expected:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break  # tick budget elapsed — stop waiting; absent sources are timeouts
        try:
            name, outcome = out_q.get(timeout=remaining)
        except queue.Empty:
            break
        results[name] = outcome
    return results


def crawl_tick(
    content_dir,
    state_dir,
    cfg,
    universe: list[str],
    now: datetime,
    client=None,
    max_workers: int = 6,
    registry: dict[str, SourceAdapter] | None = None,
    tick_budget_s: float = 90.0,
) -> CrawlResult:
    """Run one crawl tick and return its :class:`CrawlResult`.

    Steps, in order:

    1. Load :class:`~futures_fund.source_health.SourceHealth` from ``state_dir``.
    2. Select the enabled adapters (config ``sources`` block), then drop any whose circuit breaker is
       open at ``now`` — those go straight to ``degraded`` and are NOT run.
    3. If ``client`` is None the fetch would be a silent no-op for every adapter, so the tick records
       a hard error per source (``record_err``) and runs nothing — it never ``record_ok``-stamps a
       dead/unconfigured client as a healthy upstream. Otherwise fan the healthy adapters out over a
       bounded :class:`ThreadPoolExecutor` (``max_workers``),
       each handed the injected ``client``. Time each; on success ``record_ok`` (with latency) and
       collect its items; on ANY exception ``record_err`` (eventual circuit-break) and record the
       error string. A raising/hanging adapter is isolated to its own future AND bounded by a
       wall-clock ``tick_budget_s`` deadline: a future still running when the budget elapses is
       recorded as a (timeout) error via ``record_err`` rather than blocking the tick, and the pool
       is torn down with ``wait=False`` so a stuck worker thread can never pin the tick forever.
    4. ``store_items`` the union (deduped) -> the NEW items; tally raw per-source counts.
    5. For every coin touched by a NEW item, ``update_digest``.
    6. ``purge`` the store at ``now`` (evict past the retention window).
    7. ``save_health`` and return the :class:`CrawlResult`.

    ``registry`` lets callers (and tests) inject a custom ``{name: adapter}`` map; it defaults to the
    shared :data:`~futures_fund.sources.SOURCE_ADAPTERS`.
    """
    reg = registry if registry is not None else SOURCE_ADAPTERS

    health = source_health.load_health(state_dir)
    enabled = enabled_adapters(cfg, reg)

    healthy: list[SourceAdapter] = []
    degraded: list[str] = []
    for adapter in enabled:
        if source_health.is_healthy(health, adapter.name, now):
            healthy.append(adapter)
        else:
            degraded.append(adapter.name)

    per_source: dict[str, int] = {}
    errors: dict[str, str] = {}
    all_items: list[ContentItem] = []

    if healthy and client is None:
        # A None client makes every adapter a silent no-op (get_bytes/get_json short-circuit to
        # None) yet would still report "success" -> stamping dead sources HEALTHY on a no-op tick.
        # Treat a missing client as a hard error for every source instead of running the no-op: this
        # is a misconfiguration, not a healthy upstream, so it must NOT clear failure streaks.
        for adapter in healthy:
            source_health.record_err(health, adapter.name, now)
            errors[adapter.name] = "ValueError: crawl_tick called with client=None (no-op fetch)"
            per_source[adapter.name] = 0
    elif healthy:
        results = _fan_out(healthy, client, cfg, universe, max_workers, tick_budget_s)
        for adapter in healthy:
            outcome = results.get(adapter.name)
            if outcome is None or outcome[0] is None:
                # hung past the tick budget, OR raised: record an error + a health failure.
                source_health.record_err(health, adapter.name, now)
                errors[adapter.name] = (
                    outcome[2] if outcome is not None
                    else f"TimeoutError: adapter exceeded tick budget of {tick_budget_s:.0f}s"
                )
                per_source[adapter.name] = 0
                continue
            items, elapsed_ms, _err = outcome
            source_health.record_ok(health, adapter.name, elapsed_ms, now)
            per_source[adapter.name] = len(items)
            all_items.extend(items)

    new_items = content_store.store_items(content_dir, all_items)

    touched: list[str] = []
    seen_coins: set[str] = set()
    for item in new_items:
        for coin in item.coins:
            key = coin.upper()
            if key not in seen_coins:
                seen_coins.add(key)
                touched.append(key)
    for coin in touched:
        content_store.update_digest(content_dir, coin, now)

    content_store.purge(content_dir, now)

    source_health.save_health(state_dir, health)

    return CrawlResult(
        new_items=len(new_items),
        per_source=per_source,
        errors=errors,
        degraded=sorted(degraded),
    )


__all__ = ["CrawlResult", "crawl_tick"]
