"""Offline tests for the standing self-audit panel (Pillar 4 — AUDIT).

The panel PASSES on a healthy crafted state (digests reference real items; the latest completed
cycle carries an auditor verdict) and FAILS when a load-bearing invariant breaks:

  * pacing PRESSES while in drawdown (anti-martingale regression — injected via monkeypatch);
  * a COMPLETED cycle (has report.json) lacks an auditor.json (an ungated execution);
  * a coin digest references an item that does not resolve in the store (dangling ground truth);
  * the gate RR floor drops below HARD_MIN_RR.

All checks run against INJECTED state/content dirs with no network and no live clock.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime

from futures_fund import content_store, pacing, rr_floor, self_audit
from futures_fund.content_store import ContentItem, make_id
from futures_fund.cycle_io import save_output
from futures_fund.pacing import PacingState

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- helpers


def _seed_healthy_content(content_dir):
    """Store two real items for BTC and recompute the digest so its top_item_ids resolve."""
    items = [
        ContentItem(id=make_id("rss", f"http://a/{i}", f"BTC news {i}"), source="rss",
                    feed="f", url=f"http://a/{i}", title=f"BTC news {i}", body="b",
                    published_ts=NOW, fetched_ts=NOW, coins=["BTC"], item_sentiment="positive")
        for i in range(2)
    ]
    content_store.store_items(content_dir, items)
    content_store.update_digest(content_dir, "BTC", NOW)


def _seed_completed_cycle(state_dir, cycle, *, with_auditor=True):
    """A 'completed' cycle = one with a report.json. Optionally also write the auditor verdict."""
    save_output(state_dir, cycle, "report", {"cycle": cycle, "fills": []})
    if with_auditor:
        save_output(state_dir, cycle, "auditor", {"passed": True, "checks": [], "mismatches": []})


# --------------------------------------------------------------------------- PASS on healthy state


def test_self_audit_passes_on_healthy_state(tmp_path):
    state = tmp_path / "state"
    content = tmp_path / "content"
    _seed_healthy_content(content)
    _seed_completed_cycle(state, 12, with_auditor=True)

    res = self_audit.run_self_audit(state_dir=state, content_dir=content)

    assert res["ok"] is True, [c for c in res["checks"] if not c["ok"]]
    assert all(c["ok"] for c in res["checks"])
    # every named invariant is present in the panel
    names = {c["name"] for c in res["checks"]}
    assert {"pacing.anti_martingale", "gate.rr_floor",
            "content_store.digests_reference_existing_items",
            "auditor.latest_completed_cycle_gated", "price.card_non_directional"} <= names


# --------------------------------------------------------------- FAIL: pacing presses in drawdown


def test_self_audit_fails_when_pacing_presses_in_drawdown(tmp_path, monkeypatch):
    state = tmp_path / "state"
    content = tmp_path / "content"
    _seed_healthy_content(content)
    _seed_completed_cycle(state, 1, with_auditor=True)

    # Inject the anti-martingale REGRESSION: pacing now returns 'press' even in drawdown.
    def _broken(*, drawdown, **_kw):
        mode = "press"  # the bug: doubling into losses
        return PacingState(
            mode=mode, appetite=0.9, suggested_risk_mult=1.0, mtd_return=-0.04, pace=0.025,
            pace_gap=-0.065, drawdown=drawdown, open_heat=0.0, in_drawdown=drawdown >= 0.05,
            days_elapsed=15, days_in_month=30, directive="PRESS",
        )

    monkeypatch.setattr(pacing, "compute_pacing", _broken)

    res = self_audit.run_self_audit(state_dir=state, content_dir=content)
    assert res["ok"] is False
    fail = next(c for c in res["checks"] if c["name"] == "pacing.anti_martingale")
    assert fail["ok"] is False and "press" in fail["detail"]


# ------------------------------------------------------------------ FAIL: completed cycle ungated


def test_self_audit_fails_when_completed_cycle_lacks_auditor(tmp_path):
    state = tmp_path / "state"
    content = tmp_path / "content"
    _seed_healthy_content(content)
    # an earlier gated cycle, then a LATER completed cycle with NO auditor.json (ungated execution).
    _seed_completed_cycle(state, 4, with_auditor=True)
    _seed_completed_cycle(state, 5, with_auditor=False)

    res = self_audit.run_self_audit(state_dir=state, content_dir=content)
    assert res["ok"] is False
    fail = next(c for c in res["checks"] if c["name"] == "auditor.latest_completed_cycle_gated")
    assert fail["ok"] is False and "5" in fail["detail"]


def test_self_audit_auditor_check_vacuous_when_no_completed_cycle(tmp_path):
    state = tmp_path / "state"
    content = tmp_path / "content"
    _seed_healthy_content(content)
    # a cycle dir exists but has only an auditor (no report.json) -> not 'completed' -> vacuous OK
    save_output(state, 8, "auditor", {"passed": True, "checks": [], "mismatches": []})

    res = self_audit.run_self_audit(state_dir=state, content_dir=content)
    check = next(c for c in res["checks"] if c["name"] == "auditor.latest_completed_cycle_gated")
    assert check["ok"] is True


# --------------------------------------------------------------------------- FAIL: dangling digest


def test_self_audit_fails_on_dangling_digest_reference(tmp_path):
    state = tmp_path / "state"
    content = tmp_path / "content"
    _seed_completed_cycle(state, 1, with_auditor=True)
    # write a digest by hand pointing at an item id that was never stored.
    ddir = content_store._digests_dir(content)
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / "BTC.json").write_text(json.dumps({
        "coin": "BTC", "top_item_ids": ["deadbeef-never-stored"], "rolling_s": 0.0,
    }))

    res = self_audit.run_self_audit(state_dir=state, content_dir=content)
    assert res["ok"] is False
    fail = next(c for c in res["checks"]
                if c["name"] == "content_store.digests_reference_existing_items")
    assert fail["ok"] is False and "deadbeef-never-stored" in fail["detail"]


# ------------------------------------------------------------------ FAIL: RR floor below hard min


def test_self_audit_fails_when_rr_floor_below_hard_min(tmp_path, monkeypatch):
    state = tmp_path / "state"
    content = tmp_path / "content"
    _seed_healthy_content(content)
    _seed_completed_cycle(state, 1, with_auditor=True)

    # Inject a relaxed effective floor below the desk's HARD minimum (a gate-weakening regression).
    monkeypatch.setattr(rr_floor, "effective_rr_floor",
                        lambda quadrant, st: self_audit.HARD_MIN_RR - 0.5)

    res = self_audit.run_self_audit(state_dir=state, content_dir=content)
    assert res["ok"] is False
    fail = next(c for c in res["checks"] if c["name"] == "gate.rr_floor")
    assert fail["ok"] is False


# --------------------------------------------------------------------------- pure invariants


def test_pure_price_card_invariant():
    ok, _detail = self_audit.invariant_price_card_is_non_directional()
    assert ok is True


def test_pure_auditor_leak_invariant():
    ok, _detail = self_audit.invariant_auditor_vetoes_price_leak()
    assert ok is True
