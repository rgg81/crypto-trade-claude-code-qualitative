"""Offline tests for the thin crawler-loop CLIs: crawl_due_check.py + purge_cli.py.

No network, no clock dependence beyond the real wall clock for the due-check cold-start (which is
DUE regardless of time). We assert the printed contract (DUE:/SKIP: lines, purge counts) and exit
codes.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund import content_store
from futures_fund.content_store import ContentItem, make_id

# --------------------------------------------------------------------------- crawl_due_check


def test_crawl_due_check_cold_start_due(tmp_path, capsys):
    from scripts.crawl_due_check import main
    rc = main(["--state-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("DUE:")


def test_crawl_due_check_skips_after_stamp(tmp_path, capsys):
    from futures_fund.scheduling import floor_n, stamp_crawl
    from scripts.crawl_due_check import main
    # stamp the CURRENT slot so the immediate re-poll SKIPs
    now = datetime.now(UTC)
    stamp_crawl(tmp_path, floor_n(now, 15))
    rc = main(["--state-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("SKIP:")


# --------------------------------------------------------------------------- purge_cli


def _item(*, published):
    return ContentItem(
        id=make_id("rss", f"http://{published.isoformat()}", "t"),
        source="rss", feed="f", url=f"http://{published.isoformat()}", title="t",
        published_ts=published, fetched_ts=published, coins=["BTC"],
    )


def test_purge_cli_drops_old_items(tmp_path, capsys):
    from scripts.purge_cli import main
    content_dir = tmp_path / "content"
    old = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)        # well outside 30d
    recent = datetime(2026, 6, 13, 0, 0, 0, tzinfo=UTC)
    content_store.store_items(content_dir, [_item(published=old), _item(published=recent)])

    # run_purge directly to pin `now` deterministically, then check the count surfaced by main
    from scripts.purge_cli import run_purge
    res = run_purge(str(content_dir), 30, datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC))
    assert res["files_deleted"] >= 1
    assert res["pointers_dropped"] >= 1

    # main on an empty dir is a clean no-op with exit 0
    rc = main(["--content-dir", str(tmp_path / "empty")])
    out = capsys.readouterr().out
    assert rc == 0 and out.startswith("purge ok:")


def test_purge_cli_empty_store_noop(tmp_path, capsys):
    from scripts.purge_cli import main
    rc = main(["--content-dir", str(tmp_path / "content")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "files_deleted=0" in out and "pointers_dropped=0" in out


def test_purge_cli_result_shape(tmp_path):
    from scripts.purge_cli import run_purge
    res = run_purge(str(tmp_path / "content"), 30, datetime.now(UTC))
    assert set(res) == {"files_deleted", "pointers_dropped"}
    assert res == {"files_deleted": 0, "pointers_dropped": 0}


def test_last_crawl_state_shape(tmp_path):
    from futures_fund.scheduling import stamp_crawl
    p = stamp_crawl(tmp_path, datetime(2026, 6, 13, 12, 7, 0, tzinfo=UTC))
    payload = json.loads(p.read_text())
    assert set(payload) >= {"last_slot", "stamped_at", "interval_min"}
