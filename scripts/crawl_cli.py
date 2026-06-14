"""Run ONE 15-minute crawl tick — the deterministic ingest heartbeat of Operation ORACLE.

A tick:

  1. Builds the WORKING UNIVERSE = config ``qualitative.core_watchlist`` + attention-spiking coins
     (:func:`futures_fund.attention.detect_spikes` over the existing on-disk per-coin digests).
  2. Constructs a real, TIMEOUT-bounded ``httpx.Client`` (injectable for tests).
  3. Calls :func:`futures_fund.crawler.crawl_tick`, which fans out the enabled+healthy source
     adapters, dedupes + stores new :class:`~futures_fund.content_store.ContentItem`s, refreshes the
     per-coin digests, purges past the retention window, and persists source health.
  4. Writes ``content/_pending_summaries.json`` — the list of items still lacking a summary
     (``item_sentiment is None``) for the universe coins, as compact
     ``{item_id, source, title, body_excerpt, coins}`` dicts. This is the WORK QUEUE the Sonnet
     summarizer SKILL reads; the crawler never calls a model.
  5. Stamps ``state/crawl/last_crawl.json`` so the next poll in this slot SKIPs.

Everything network/clock related is injected so the tick is deterministic and trivially testable
offline. Prints a one-line result. Exit 0 on success, 1 on a hard failure.

    uv run python scripts/crawl_cli.py
    uv run python scripts/crawl_cli.py --config config.yaml --state-dir state --content-dir content
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from futures_fund import content_store, crawler
from futures_fund.config import load_settings
from futures_fund.content_store import _digests_dir, get_item
from futures_fund.universe import build_universe

# Lookback when collecting still-unsummarized items for the summarizer queue. Generous (wider than
# one crawl slot) so a backlog left by a prior failed summarizer pass is re-queued, not stranded —
# bounded by the 30-day retention window the store already enforces.
_PENDING_LOOKBACK_DAYS = 7
# Body excerpt sent to the summarizer queue — enough context to summarise, small enough to keep the
# work-queue file compact (the full body stays in the store; only the LLM input is trimmed here).
_BODY_EXCERPT_CHARS = 600


def _atomic_write_text(path: Path, text: str) -> None:
    """temp-file + os.replace atomic write (the content_store / cycle_io pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _load_all_digests(content_dir) -> dict[str, dict]:
    """Read every persisted per-coin digest (``content/digests/<COIN>.json``) into a map.

    Fail-soft: a missing dir -> {}, a torn/garbage digest file is skipped (never crashes the tick).
    Feeds the attention spike scan that augments the core watchlist."""
    out: dict[str, dict] = {}
    ddir = _digests_dir(content_dir)
    if not ddir.exists():
        return out
    for path in sorted(ddir.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        if isinstance(data, dict):
            out[path.stem.upper()] = data
    return out


def _make_cfg(settings) -> dict:
    """Build the crawler config dict from Settings.

    ``crawl_tick`` reads two keys off cfg: ``sources`` (the enabled-adapter NAME list consumed by
    :func:`enabled_adapters`) and ``sources_config`` (per-source upstream dicts handed to each
    adapter via :func:`crawler._source_cfg`). We project the structured
    :class:`~futures_fund.config.SourcesSettings` onto that shape; adapters read their keys
    defensively, so handing every source the same feeds/subs/mirrors/channels block is safe."""
    src = settings.sources
    per_source = {
        "feeds": list(src.feeds),
        "subs": list(src.subs),
        "mirrors": list(src.mirrors),
        "channels": list(src.channels),
        # common aliases the keyless adapters also accept
        "subreddits": list(src.subs),
        "instances": list(src.mirrors),
    }
    return {
        "sources": list(src.enabled),
        "sources_config": {name: per_source for name in src.enabled},
    }


def _collect_pending(content_dir, coins: list[str], now: datetime) -> list[dict]:
    """Gather items still lacking a summary (``item_sentiment is None``) for ``coins``.

    Walks each coin's pointer index for the lookback window, hydrates the unique items, and keeps
    only the un-summarized ones. Deterministic: de-duped by id, sorted by (published_ts, id) so the
    work-queue is stable across runs. Each entry is the compact LLM input the summarizer needs."""
    since = now - timedelta(days=_PENDING_LOOKBACK_DAYS)
    seen: set[str] = set()
    pending: list = []
    for coin in coins:
        for ptr in content_store.index_for_coin(content_dir, coin, since):
            iid = ptr.get("item_id")
            if not iid or iid in seen:
                continue
            seen.add(iid)
            item = get_item(content_dir, iid)
            if item is None or item.item_sentiment is not None or item.summary is not None:
                continue
            pending.append(item)
    pending.sort(key=lambda it: (content_store._as_utc(it.published_ts).isoformat(), it.id))
    return [
        {
            "item_id": it.id,
            "source": it.source,
            "title": it.title,
            "body_excerpt": (it.body or "")[:_BODY_EXCERPT_CHARS],
            "coins": list(it.coins),
        }
        for it in pending
    ]


def _build_client(timeout_s: float):
    """Construct a real, timeout-bounded httpx.Client. Connect/read/write/pool timeouts are all
    capped so no single dead upstream can hang the tick beyond the adapter+tick budgets."""
    import httpx

    return httpx.Client(
        timeout=httpx.Timeout(timeout_s, connect=timeout_s),
        follow_redirects=True,
        headers={"User-Agent": "OracleDesk research; keyless public read"},
    )


def run_crawl(
    *,
    config_path: str = "config.yaml",
    state_dir: str = "state",
    content_dir: str = "content",
    now: datetime | None = None,
    client=None,
    client_factory=None,
    crawl_fn=None,
    settings=None,
) -> dict:
    """Run one crawl tick end to end and return a result dict. Injectable for offline tests.

    ``client`` (pre-built) takes priority; else ``client_factory(timeout_s)`` builds one; else a
    real httpx.Client is constructed. ``crawl_fn`` defaults to :func:`crawler.crawl_tick`. The built
    client is always closed in a ``finally``. Writes ``content/_pending_summaries.json`` and stamps
    ``state/crawl/last_crawl.json``; returns ``{universe, result, pending, pending_path}``."""
    from futures_fund.scheduling import stamp_crawl

    now = now or datetime.now(UTC)
    settings = settings or load_settings(config_path)
    q = settings.qualitative

    digests = _load_all_digests(content_dir)
    uni = build_universe(
        list(q.core_watchlist), digests, now,
        ratio=q.spike_ratio, min_mentions=q.spike_min_mentions,
    )
    universe = uni["all"]

    cfg = _make_cfg(settings)
    crawl = crawl_fn or crawler.crawl_tick

    built_client = None
    if client is None:
        factory = client_factory or _build_client
        built_client = factory(8.0)
        client = built_client
    try:
        result = crawl(content_dir, state_dir, cfg, universe, now, client=client)
    finally:
        if built_client is not None:
            try:
                built_client.close()
            except Exception:  # noqa: BLE001 — best-effort close; never mask the tick outcome
                pass

    pending = _collect_pending(content_dir, universe, now)
    pending_path = Path(content_dir) / "_pending_summaries.json"
    _atomic_write_text(pending_path, json.dumps(pending, indent=2, default=str))

    stamp_crawl(state_dir, now, interval_min=q.crawl_interval_min)

    res = result.model_dump() if hasattr(result, "model_dump") else dict(result)
    return {
        "universe": uni,
        "result": res,
        "pending": pending,
        "pending_path": str(pending_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one 15-minute crawl tick.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument("--content-dir", default="content")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        out = run_crawl(
            config_path=args.config,
            state_dir=args.state_dir,
            content_dir=args.content_dir,
        )
    except Exception as e:  # noqa: BLE001 — a hard tick failure: surface it, exit non-zero
        print(f"CRAWL ERROR: {e!r}")
        return 1

    r = out["result"]
    uni = out["universe"]
    print(
        f"crawl ok: universe={len(uni['all'])} (core={len(uni['core'])} "
        f"spiking={len(uni['spiking'])}) new_items={r.get('new_items', 0)} "
        f"degraded={len(r.get('degraded', []))} pending={len(out['pending'])} "
        f"-> {out['pending_path']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
