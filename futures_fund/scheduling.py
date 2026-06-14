"""Due-gate for the hourly-poll loop: decide whether THIS 4h candle still needs a cycle.

The desk wants exactly one cycle per 4h Binance candle (UTC grid 00/04/08/12/16/20). The
session-only cron fires only while the REPL is idle, so a tick landing mid-cycle is skipped and
never replays. To make a skipped boundary self-heal, the cron polls HOURLY and gates here:

    run iff no completed cycle has yet SERVED the candle that contains `now`.

Design notes (vetted by the design red-team, see tests/test_scheduling.py):
  * The cadence primitive is the SERVED CANDLE — report['candle'] = floor4(gate-start instant) —
    NOT completion time. A catch-up that finishes after the next boundary still only serves the
    candle it started in, so it cannot "steal" the next candle.
  * "Last completed cycle" = the highest cycle number whose report.json EXISTS and PARSES, found
    by scanning dirs in DESCENDING order. Never max(dir): a phantom empty dir or a crashed
    pre-gate dir must not wedge the loop into permanent SKIP.
  * All datetimes are tz-aware UTC end to end. mtime fallback uses fromtimestamp(ts, tz=UTC);
    ran_at/candle parsing normalizes 'Z' and coerces any naive value to UTC. floor4 asserts aware.
  * Fail-safe: any unhandled error returns DUE (an extra run is low-harm — the gate reconciles
    against on-disk positions and cannot double-open — whereas a swallowed candle is worse).

Returns (mode, n, reason):
  mode == 'FRESH'  -> run a brand-new cycle, create state/cycle/<n>/ (n = highest_dir + 1)
  mode == 'RETRY'  -> re-run/overwrite the crashed dir state/cycle/<n>/ (n = highest_dir)
  mode == 'SKIP'   -> this candle is already served; do nothing (n = the serving cycle)
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
_CANDLE = timedelta(hours=4)
# Tolerate a served candle up to ONE step ahead of now's boundary, then distrust as corrupt.
# WHY: under a correct monotonic clock a served candle is always <= now's boundary
# (candle = floor4(start) <= floor4(now)), so this tolerance is dormant in normal operation. It
# only engages on a clock anomaly. A sub-candle backward NTP step across a boundary makes the
# JUST-served candle look one step ahead; trusting it yields a bounded SKIP (correct — don't
# re-serve it) instead of a needless re-run. COST: a LARGER (>=4h) backward step or a >=4h forward
# write-skew that survives correction can false-SKIP and swallow up to two real candles before it
# self-clears. That is an accepted, bounded, self-healing tradeoff for a paper desk; tighten this
# toward a few minutes if even that bounded swallow is unacceptable (then re-derive the
# clock_moved_backward test, whose backstep would flip to a harmless DUE re-run).
_FUTURE_TOL = _CANDLE


def floor4(dt: datetime) -> datetime:
    """Floor a tz-aware UTC datetime to the 4h candle grid (00/04/08/12/16/20)."""
    assert dt.tzinfo is not None, "floor4 requires a tz-aware datetime"
    return dt.replace(hour=(dt.hour // 4) * 4, minute=0, second=0, microsecond=0)


def _parse_utc(raw) -> datetime | None:
    """Parse an ISO timestamp to an aware-UTC datetime, or None. Never raises."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    # Deliver UTC as the docstring promises: normalize any foreign offset (e.g. +05:30) to UTC,
    # and treat a naive stamp as already-UTC. Either way floor4 then sees a true-UTC instant.
    return dt.astimezone(UTC) if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _served_candle(report_path: Path, now_utc: datetime) -> datetime | None:
    """Resolve which candle a completed cycle served, from its report.json. Priority:
    report['candle'] -> floor4(report['ran_at']) -> floor4(file mtime). All tz-aware UTC.
    A ran_at in the future (clock skew) is discarded so it cannot drive the candle. Returns None
    if the report cannot be read/parsed (caller treats that dir as not-completed)."""
    try:
        rep = json.loads(report_path.read_text())
    except (json.JSONDecodeError, OSError, ValueError):
        return None
    if not isinstance(rep, dict):
        return None  # valid JSON but not an object (null/list/scalar) == not a completed cycle
    ran_at = _parse_utc(rep.get("ran_at"))
    if ran_at is not None and ran_at > now_utc:
        ran_at = None  # future-stamp guard: never let a skewed ran_at wedge the loop
    cand = _parse_utc(rep.get("candle"))
    if cand is None and ran_at is not None:
        cand = floor4(ran_at)
    if cand is None:
        try:
            cand = floor4(datetime.fromtimestamp(report_path.stat().st_mtime, tz=UTC))
        except OSError:
            return None
    return cand


def cycle_due(state_dir, now_utc: datetime) -> tuple[str, int, str]:
    """Decide whether the candle containing `now_utc` still needs a cycle. Never raises."""
    try:
        assert now_utc.tzinfo is not None and now_utc.utcoffset() == timedelta(0), \
            "now_utc must be tz-aware UTC"
        boundary = floor4(now_utc)
        cyc = Path(state_dir) / "cycle"

        dirs = sorted(
            (int(p.name) for p in cyc.glob("*") if p.is_dir() and p.name.isdigit()),
            reverse=True,
        ) if cyc.exists() else []
        if not dirs:
            return ("FRESH", 1, "cold-start: no cycle dirs")
        highest_dir = dirs[0]

        completed_n: int | None = None
        served: datetime | None = None
        for n in dirs:
            rp = cyc / str(n) / "report.json"
            if not rp.exists():
                continue  # crashed/in-flight: not a completed cycle
            cand = _served_candle(rp, now_utc)
            if cand is None:
                continue  # unparseable report == not completed
            if cand > boundary + _FUTURE_TOL:
                continue  # egregiously-future candle (corrupt/skew) -> distrust, scan downward
            completed_n, served = n, cand
            break

        if completed_n is None or served is None:
            # No trustworthy completed cycle. The highest dir is a crashed/junk attempt -> RETRY it
            # (overwrite). Safe: the gate reconciles vs on-disk positions and cannot double-open.
            return ("RETRY", highest_dir, f"no completed cycle; retry/overwrite dir {highest_dir}")

        if served >= boundary:
            nxt = (boundary + _CANDLE).isoformat()
            return ("SKIP", completed_n,
                    f"cycle {completed_n} already served candle {served.isoformat()} "
                    f"(>= boundary {boundary.isoformat()}); next boundary {nxt}")

        # This candle is unserved -> DUE. If a higher dir exists with no trustworthy report, it is
        # a crashed current-candle attempt -> RETRY/overwrite it; otherwise a FRESH next cycle.
        if highest_dir > completed_n:
            return ("RETRY", highest_dir,
                    f"cycle {highest_dir} crashed before gate; last completed {completed_n} "
                    f"served {served.isoformat()}")
        return ("FRESH", highest_dir + 1,
                f"new candle {boundary.isoformat()}; last completed {completed_n} "
                f"served {served.isoformat()}")
    except Exception as e:  # noqa: BLE001 — fail SAFE: never swallow a candle on an internal error
        return ("FRESH", 1, f"fail-safe DUE after internal error: {e!r}")


# --------------------------------------------------------------------------- #
# 15-minute crawler-loop gate                                                  #
#                                                                              #
# The crawler heartbeat (futures_fund.crawler.crawl_tick) wants exactly one    #
# tick per N-minute grid slot (default 15-min: :00/:15/:30/:45). A session-only #
# cron may fire at any wall-clock instant, so we gate on the SLOT, not the      #
# fire time, and remember the last-served slot in state/crawl/last_crawl.json.  #
# This is the lighter sibling of cycle_due — there is no FRESH/RETRY/SKIP cycle #
# bookkeeping, just (mode, reason) with mode in {"DUE", "SKIP"}.                #
# --------------------------------------------------------------------------- #


def floor_n(dt: datetime, minutes: int) -> datetime:
    """Floor a tz-aware UTC datetime down to an N-minute grid within the hour.

    For ``minutes=15`` the slots are :00/:15/:30/:45; for ``minutes=5`` they are :00/:05/.../:55,
    etc. The grid is anchored at the top of the HOUR (not the day), which is all the crawler needs
    — slots are compared as instants so an hour rollover orders correctly. ``minutes`` must divide
    cleanly into 60 to land on a stable grid; a non-positive/garbage value is clamped to 1 so this
    never raises (a 1-minute grid is the safe degenerate). Like :func:`floor4`, requires tz-aware.
    """
    assert dt.tzinfo is not None, "floor_n requires a tz-aware datetime"
    m = int(minutes) if isinstance(minutes, (int, float)) and int(minutes) > 0 else 1
    return dt.replace(minute=(dt.minute // m) * m, second=0, microsecond=0)


def _crawl_state_path(state_dir) -> Path:
    return Path(state_dir) / "crawl" / "last_crawl.json"


def crawl_due(state_dir, now, interval_min: int = 15) -> tuple[str, str]:
    """Decide whether the crawler should run for the ``interval_min``-minute slot of ``now``.

    Run iff ``now``'s grid slot is STRICTLY LATER than the last slot we recorded as served in
    ``state/crawl/last_crawl.json`` (``{"last_slot": "<iso>", ...}``). The comparison is on the
    floored slot instant, so a second poll inside the same slot SKIPs (no thrash) while the first
    poll of a new slot is DUE — even after a multi-slot outage (a single catch-up serves the
    current slot; it does not replay every missed slot).

    Cold start (no state file / unreadable / no usable ``last_slot``) is DUE. Any unhandled error
    fails SAFE to DUE — an extra crawl tick is low-harm (it dedupes into the content store and only
    refreshes digests) whereas a swallowed slot loses tape. Returns ``(mode, reason)`` with
    ``mode`` in ``{"DUE", "SKIP"}``. Makes NO writes — the caller stamps the slot after a tick.
    """
    try:
        assert now.tzinfo is not None and now.utcoffset() == timedelta(0), \
            "now must be tz-aware UTC"
        slot = floor_n(now, interval_min)
        p = _crawl_state_path(state_dir)
        if not p.exists():
            return ("DUE", f"cold-start: no crawl state; slot {slot.isoformat()}")
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError, ValueError):
            return ("DUE", "crawl state unreadable -> fail-safe DUE")
        if not isinstance(raw, dict):
            return ("DUE", "crawl state not an object -> fail-safe DUE")
        last = _parse_utc(raw.get("last_slot"))
        if last is None:
            return ("DUE", "crawl state missing/garbage last_slot -> fail-safe DUE")
        # Normalise the stored slot to the grid too, so a hand-written off-grid stamp still compares
        # apples-to-apples (e.g. a stamp at :07 floors to the :00 slot it actually served).
        last_slot = floor_n(last, interval_min)
        if slot > last_slot:
            return ("DUE", f"new slot {slot.isoformat()} (last served {last_slot.isoformat()})")
        return ("SKIP", f"slot {slot.isoformat()} already served (last {last_slot.isoformat()})")
    except Exception as e:  # noqa: BLE001 — fail SAFE: an extra tick is low-harm, a lost slot is not
        return ("DUE", f"fail-safe DUE after internal error: {e!r}")


def stamp_crawl(state_dir, now, interval_min: int = 15) -> Path:
    """Record ``now``'s slot as the last-served crawl slot (atomic write). Returns the path.

    Called by the crawl CLI AFTER a tick so the next poll in the same slot SKIPs. The write is
    atomic (temp file + ``os.replace`` — the state.py / cycle_io.py pattern) so a concurrent
    :func:`crawl_due` reader never sees a half-written file."""
    slot = floor_n(now, interval_min)
    p = _crawl_state_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"last_slot": slot.isoformat(), "stamped_at": now.isoformat(),
               "interval_min": int(interval_min)}
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, p)
    return p
