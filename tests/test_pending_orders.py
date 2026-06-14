"""#2 trigger orders: hybrid fill (stop-entry on CLOSE break, limit-entry on TOUCH), knife guard,
wrong-side reject, no-bar unevaluable, fire-before-expiry, held-skip, corrupt-store fail-safe."""
import json

from futures_fund.pending_orders import (
    PendingOrder,
    _stale_geometry,
    check_pending_orders,
    fired_to_proposal,
    load_pending_orders,
    low_rr_triggers,
    non_crypto_triggers,
    revalidate_triggers,
    save_pending_orders,
    trigger_rr,
    upsert_triggers,
)


def _o(symbol="BTCUSDT", direction="short", kind="stop_entry", trigger=100.0, stop=105.0,
       expires=99, **kw):
    return PendingOrder(symbol=symbol, direction=direction, kind=kind, trigger_level=trigger,
                        stop=stop, take_profits=kw.get("tps", [trigger * 0.9]), atr=1.0,
                        risk_mult=kw.get("risk_mult", 1.0),
                        require_oi_rising=kw.get("require_oi_rising", False),
                        created_cycle=1, expires_cycle=expires)


def _save(tmp, orders):
    save_pending_orders(tmp, orders)


def test_stop_entry_short_fires_on_close_below_trigger(tmp_path):
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=105)])
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5)
    assert len(fired) == 1 and not remaining
    assert fired_to_proposal(fired[0])["entry"] == 100  # fills at trigger, not the 99 close


def test_stop_entry_short_no_fire_on_close_at_or_above(tmp_path):
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=105)])
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 100, "low": 99, "high": 101}}, 5)
    assert not fired and len(remaining) == 1  # strict <


def test_stop_entry_short_fires_on_genuine_breakdown_through_trigger(tmp_path):
    # the cy66 BCH case: the bar traded UP TO/through the trigger (high 101 >= 100) and CLOSED below
    # it (close 96) -> a real downward break = a live sell-stop fill at the trigger. Fires + opens.
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=105)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 96, "low": 95, "high": 101}}, 5)
    assert len(fired) == 1 and not remaining
    assert fired_to_proposal(fired[0])["entry"] == 100  # fills at the trigger price


def test_stop_entry_long_gap_up_fills_at_bar_open_not_trigger(tmp_path):
    # GAP-HONEST FILL: a long breakout stop_entry @100 fires on close 108>100, but the firing bar
    # GAPPED UP (open 105, low 104 > 100) — price never traded at 100, so a live buy-stop's first
    # fill is the gap-open 105 (worse), NOT the unattainable 100. Symmetric realism, only worsens.
    _save(tmp_path, [_o(kind="stop_entry", direction="long", trigger=100, stop=94, tps=[120])])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"open": 105, "low": 104, "high": 110, "close": 108}}, 5)
    assert len(fired) == 1 and not remaining
    assert fired_to_proposal(fired[0])["entry"] == 105   # gap-open, not the trigger 100


def test_stop_entry_short_gap_down_fills_at_bar_open_not_trigger(tmp_path):
    # mirror: a short breakdown stop_entry @100 fires on close 95<100, but the bar GAPPED DOWN
    # (open 96, high 97 < 100) — 100 never traded, so the live sell-stop's first fill is the
    # gap-open 96 (worse for a short), NOT 100.
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=106, tps=[80])])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"open": 96, "low": 94, "high": 97, "close": 95}}, 5)
    assert len(fired) == 1 and not remaining
    assert fired_to_proposal(fired[0])["entry"] == 96    # gap-open, not the trigger 100


def test_stop_entry_in_range_bar_still_fills_at_trigger_even_with_open(tmp_path):
    # the level WAS traded this bar (low 97 <= 100 <= high 108) -> a resting stop fills at 100; the
    # bar open 98 must NOT override the in-range trigger fill (favorable-but-achievable).
    _save(tmp_path, [_o(kind="stop_entry", direction="long", trigger=100, stop=94, tps=[120])])
    fired, _, _ = check_pending_orders(
        tmp_path, {"BTCUSDT": {"open": 98, "low": 97, "high": 108, "close": 105}}, 5)
    assert fired_to_proposal(fired[0])["entry"] == 100


def test_stop_entry_gap_with_nonfinite_open_falls_back_to_trigger(tmp_path):
    # a corrupt/non-finite open on a gapped bar must NOT propagate NaN into the entry (which would
    # slip the RR veto, since NaN < floor is False) -> fall back to the trigger level, fail-safe.
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=106, tps=[80])])
    fired, _, _ = check_pending_orders(
        tmp_path, {"BTCUSDT": {"open": float("nan"), "close": 95, "low": 94, "high": 97}}, 5)
    assert fired_to_proposal(fired[0])["entry"] == 100


def test_stop_entry_gap_without_open_falls_back_to_trigger(tmp_path):
    # robustness: a gapped bar with NO `open` key (legacy feed) cannot price the gap -> fall back to
    # the trigger level (prior behavior), never raise. high 97 < trigger 100 = gapped, but no open.
    _save(tmp_path, [_o(kind="stop_entry", direction="short", trigger=100, stop=106, tps=[80])])
    fired, _, _ = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 95, "low": 94, "high": 97}}, 5)
    assert fired_to_proposal(fired[0])["entry"] == 100


def test_limit_entry_long_fires_on_low_touch(tmp_path):
    _save(tmp_path, [_o(kind="limit_entry", direction="long", trigger=100, stop=95)])
    fired, _, _ = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 105, "low": 99, "high": 106}}, 5)
    assert len(fired) == 1 and fired_to_proposal(fired[0])["entry"] == 100


def test_limit_entry_knife_guard_no_fire_when_bar_pierced_stop(tmp_path):
    _save(tmp_path, [_o(kind="limit_entry", direction="long", trigger=100, stop=95)])
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 96, "low": 94, "high": 101}}, 5)
    assert not fired and not remaining  # knife: low 94 hit trigger AND stop -> consumed, not re-armed


def test_wrong_side_stop_rejected(tmp_path):
    _save(tmp_path, [_o(kind="limit_entry", direction="long", trigger=100, stop=105)])  # stop above entry
    fired, expired, remaining = check_pending_orders(tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5)
    assert not fired and not remaining  # inverted geometry -> dropped


def test_no_bar_stays_pending_unevaluable(tmp_path):
    _save(tmp_path, [_o(symbol="ZZZUSDT", expires=99)])
    fired, expired, remaining = check_pending_orders(tmp_path, {}, 5)  # no bar for ZZZ
    assert not fired and not expired and len(remaining) == 1


def test_expiry_inclusive_and_fire_precedes_expiry(tmp_path):
    a = _o(symbol="AUSDT", kind="stop_entry", direction="short", trigger=100, stop=105, expires=5)
    b = _o(symbol="BUSDT", kind="stop_entry", direction="short", trigger=100, stop=105, expires=5)
    _save(tmp_path, [a, b])
    bars = {"AUSDT": {"close": 101, "low": 100, "high": 102},  # no fire -> expires
            "BUSDT": {"close": 99, "low": 98, "high": 101}}     # fires AND at expiry -> fires
    fired, expired, remaining = check_pending_orders(tmp_path, bars, 5)
    assert {o.symbol for o in fired} == {"BUSDT"}
    assert {o.symbol for o in expired} == {"AUSDT"}
    assert not remaining


def test_held_symbol_trigger_skipped_and_removed(tmp_path):
    _save(tmp_path, [_o(symbol="BTCUSDT")])
    fired, expired, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5, held_symbols={"BTCUSDT"})
    assert not fired and not remaining and not expired  # held -> consumed/removed


def test_corrupt_store_returns_empty(tmp_path):
    (tmp_path / "pending_orders.json").write_text('[{"symbol":"BTCUSDT","direction":"short"  garbage')
    assert load_pending_orders(tmp_path) == []
    assert check_pending_orders(tmp_path, {}, 5) == ([], [], [])


def test_missing_store_cold_start_empty(tmp_path):
    assert load_pending_orders(tmp_path) == []
    assert check_pending_orders(tmp_path, {"BTCUSDT": {"close": 1}}, 5) == ([], [], [])


def test_trigger_rr_matches_gate_formula():
    # RR the gate scores at fire time: |nearest_TP - trigger| / |stop - trigger| (entry==trigger for
    # a fired stop_entry, fixed from arm to fire). cy68 HYPE 56.40/58.30/52.90 = 3.50/1.90 = 1.84.
    rr = trigger_rr(_o(direction="short", trigger=56.40, stop=58.30, tps=[52.90]))
    assert round(rr, 2) == 1.84
    assert trigger_rr(_o(direction="long", trigger=100.0, stop=95.0, tps=[115.0])) == 3.0   # 15/5
    # NEAREST tp is used (not the deepest)
    assert trigger_rr(_o(direction="long", trigger=100.0, stop=95.0, tps=[102.0, 130.0])) == 0.4
    # degenerate -> 0.0 (zero risk / no tp) = below any positive floor
    assert trigger_rr(_o(direction="short", trigger=100.0, stop=100.0, tps=[90.0])) == 0.0
    assert trigger_rr(_o(direction="short", trigger=100.0, stop=105.0, tps=[])) == 0.0


def _se(sym, direction, trigger, stop, tps, kind="stop_entry"):
    return _o(symbol=sym, direction=direction, kind=kind, trigger=trigger, stop=stop, tps=tps)


def test_low_rr_triggers_partitions_below_the_floor():
    # Pre-arm guard: a stop_entry whose arm-time RR is below the gate's MIN_RR floor is refused (it
    # would only fire then RR-veto). >= floor passes; an exactly-2.0 passes; a limit_entry is never
    # checked here. Sub-floor: HYPE 1.84. OK: BTC 12/4=3.0, ETH 10/5=2.0, XRP limit (0.2 but limit).
    hype = _se("HYPEUSDT", "short", 56.40, 58.30, [52.90])     # 1.84
    clean = _se("BTCUSDT", "short", 100.0, 104.0, [88.0])      # 3.0
    exactly2 = _se("ETHUSDT", "long", 100.0, 95.0, [110.0])    # 2.0
    limit = _se("XRPUSDT", "long", 100.0, 95.0, [101.0], kind="limit_entry")  # 0.2 but limit=pass
    sub, ok = low_rr_triggers([hype, clean, exactly2, limit], 2.0, 1e-6)
    assert {o.symbol for o in sub} == {"HYPEUSDT"}
    assert {o.symbol for o in ok} == {"BTCUSDT", "ETHUSDT", "XRPUSDT"}


def test_upsert_replaces_by_symbol_dir_kind(tmp_path):
    existing = [_o(symbol="BTCUSDT", direction="short", kind="stop_entry", trigger=100)]
    new = [_o(symbol="BTCUSDT", direction="short", kind="stop_entry", trigger=90)]  # same key, new level
    merged = upsert_triggers(existing, new)
    assert len(merged) == 1 and merged[0].trigger_level == 90


def test_fired_trigger_carries_risk_mult():
    # a PendingOrder's risk_mult must survive into the fired AgentProposal dict (default 1.0)
    from futures_fund.pending_orders import PendingOrder, fired_to_proposal
    o = PendingOrder(symbol="ENAUSDT", direction="short", kind="stop_entry",
                     trigger_level=0.09, stop=0.0995, take_profits=[0.0691], atr=0.0095,
                     risk_mult=0.5)
    assert fired_to_proposal(o)["risk_mult"] == 0.5
    o2 = PendingOrder(symbol="BTCUSDT", direction="long", kind="stop_entry",
                      trigger_level=100.0, stop=95.0, take_profits=[110.0], atr=2.0)
    assert fired_to_proposal(o2)["risk_mult"] == 1.0


# --- OI-confirmation gate (require_oi_rising) -------------------------------------------------
# A stop_entry that OPTS IN (require_oi_rising=True) may fire on its price-close break ONLY IF OI is
# rising (fresh fuel) at fire time; a spent-OI break is a bounce-trap and HOLDS the trigger armed.
# Default False = today's behavior (OI never consulted). Symmetric: identical for long and short.

def test_oi_gate_short_fires_when_oi_rising(tmp_path):
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": 0.10})
    assert len(fired) == 1 and not remaining


def test_oi_gate_short_holds_when_oi_bleeding(tmp_path):
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": -0.09})
    assert not fired and len(remaining) == 1  # break printed but OI spent -> stays armed


def test_oi_gate_long_fires_when_oi_rising(tmp_path):
    _save(tmp_path, [_o(direction="long", trigger=100, stop=95, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 101, "low": 99, "high": 102}}, 5,
        oi_change_by_symbol={"BTCUSDT": 0.10})
    assert len(fired) == 1 and not remaining  # mirror of the short -> locks long/short symmetry


def test_oi_gate_long_holds_when_oi_bleeding(tmp_path):
    _save(tmp_path, [_o(direction="long", trigger=100, stop=95, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 101, "low": 99, "high": 102}}, 5,
        oi_change_by_symbol={"BTCUSDT": -0.09})
    assert not fired and len(remaining) == 1


def test_oi_gate_default_no_op_ignores_oi(tmp_path):
    # require_oi_rising defaults False -> OI never consulted; fires even with bleeding/absent OI
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105)])
    fired, _, _ = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": -0.50})
    assert len(fired) == 1
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105)])
    fired2, _, _ = check_pending_orders(  # no oi arg at all (the ~40 existing call sites)
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5)
    assert len(fired2) == 1


def test_oi_gate_failsafe_holds_on_missing_or_nan_oi(tmp_path):
    # require_oi_rising + missing/None/NaN OI -> fail-closed: hold armed, never a spurious fire,
    # applied IDENTICALLY to long and short (a feed outage cannot create one-sided bias).
    for oi_arg in (None, {}, {"BTCUSDT": None}, {"BTCUSDT": float("nan")}):
        for direction, close, stop in (("short", 99, 105), ("long", 101, 95)):
            _save(tmp_path, [_o(direction=direction, trigger=100, stop=stop,
                                require_oi_rising=True)])
            fired, _, remaining = check_pending_orders(
                tmp_path, {"BTCUSDT": {"close": close, "low": 98, "high": 102}}, 5,
                oi_change_by_symbol=oi_arg)
            assert not fired and len(remaining) == 1, (oi_arg, direction)


def test_oi_gate_symmetry_feed_outage_holds_both_long_and_short(tmp_path):
    # market-neutral invariant (HARD RULE 5): on ONE feed outage for a symbol, an opted-in LONG and
    # SHORT on that SAME symbol must BOTH hold — no asymmetric suppression. long_trigger < close <
    # short_trigger so both price-breaks are satisfied; the OI gate (None -> hold) suppresses both.
    _save(tmp_path, [
        _o(direction="long", kind="stop_entry", trigger=98, stop=92, require_oi_rising=True),
        _o(direction="short", kind="stop_entry", trigger=102, stop=108, require_oi_rising=True),
    ])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 100, "low": 99, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": None})
    assert not fired and len(remaining) == 2


def test_oi_gate_flat_oi_does_not_fire(tmp_path):
    # flat OI (0.0) is below the +0.5% rising deadband -> not 'rising' -> hold
    _save(tmp_path, [_o(direction="short", trigger=100, stop=105, require_oi_rising=True)])
    fired, _, remaining = check_pending_orders(
        tmp_path, {"BTCUSDT": {"close": 99, "low": 98, "high": 101}}, 5,
        oi_change_by_symbol={"BTCUSDT": 0.0})
    assert not fired and len(remaining) == 1


def test_oi_gate_field_persists_roundtrip(tmp_path):
    save_pending_orders(tmp_path, [_o(direction="short", trigger=100, stop=105,
                                      require_oi_rising=True)])
    reloaded = load_pending_orders(tmp_path)
    assert len(reloaded) == 1 and reloaded[0].require_oi_rising is True
    # a LEGACY record without the field validates with require_oi_rising == False (back-compat)
    legacy = {"symbol": "ETHUSDT", "direction": "short", "kind": "stop_entry",
              "trigger_level": 100.0, "stop": 105.0, "take_profits": [90.0], "atr": 1.0,
              "created_cycle": 1, "expires_cycle": 99}
    (tmp_path / "pending_orders.json").write_text(json.dumps([legacy]))
    assert load_pending_orders(tmp_path)[0].require_oi_rising is False


# ---- Stale-trigger geometry revalidation: a stop_entry whose swing anchor CROSSED PAST its level
# since arming (the cy43 ETH inversion) must be judged STALE -> auto-canceled. Symmetric, fail-safe.
# Only a trigger that recorded its arm-time anchor (a real breakdown/breakout level) is checked --

def _staleable(symbol="ETHUSDT", direction="short", trigger=1532.0, stop=1576.0, atr=64.1,
               anchor=1538.0, kind="stop_entry"):
    # a stop_entry armed as a genuine swing anchor: short anchored at/above its level (breakdown),
    # long at/below (breakout). anchor = the directional swing captured at ARM time.
    o = _o(symbol=symbol, direction=direction, kind=kind, trigger=trigger, stop=stop)
    return o.model_copy(update={"atr": atr, "anchor_swing": anchor})


def test_stale_geometry_cy43_eth_short_is_stale():
    # REAL cy43 numbers: armed cy42 with swing_low 1538 (>= trigger 1532), atr 64.1. By cy43 the
    # swing_low FELL to 1503.6 (< trigger): support crossed below the breakdown level -> would now
    # fire mid-bounce, not on a new low -> STALE.
    o = _staleable(trigger=1532.0, anchor=1538.0, atr=64.1)
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1503.6) is True


def test_stale_geometry_short_healthy_when_support_has_not_crossed():
    # support still at/above the breakdown level (1538 ~ unchanged) -> a clean live trigger -> keep
    o = _staleable(trigger=1532.0, anchor=1538.0, atr=64.1)
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1538.0) is False


def test_stale_geometry_unstamped_trigger_never_stale():
    # the OI-gate / legacy / non-swing-anchored case: no anchor_swing recorded -> NEVER revalidated,
    # even when the current swing_low sits far below the level (prevents canceling valid trades).
    o = _o(symbol="ETHUSDT", direction="short", trigger=1532.0, stop=1576.0)  # anchor_swing=None
    o = o.model_copy(update={"atr": 64.1})
    assert o.anchor_swing is None
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1200.0) is False


def test_stale_geometry_long_mirror_is_stale():
    # breakout LONG armed with swing_high 1900 (<= trigger 1900); resistance ROSE to 1990 (atr 60,
    # buffer 15) -> 1990 > 1900+15 -> resistance crossed above the breakout level -> stale (mirror).
    o = _staleable(direction="long", trigger=1900.0, stop=1820.0, atr=60.0, anchor=1900.0)
    assert _stale_geometry(o, swing_high=1990.0, swing_low=1500.0) is True


def test_stale_geometry_long_healthy_when_resistance_has_not_crossed():
    o = _staleable(direction="long", trigger=1900.0, stop=1820.0, atr=60.0, anchor=1900.0)
    assert _stale_geometry(o, swing_high=1880.0, swing_low=1500.0) is False


def test_stale_geometry_within_buffer_wobble_not_stale():
    # short trigger 1532, atr 64.1 -> buffer = 0.25*64.1 = 16.03, line = 1515.97. swing_low 1520 is
    # still ABOVE the line -> a noise wobble, NOT a confirmed crossing -> NOT stale.
    o = _staleable(trigger=1532.0, anchor=1538.0, atr=64.1)
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1520.0) is False


def test_stale_geometry_limit_entry_never_stale():
    # limit_entry is a pullback TOUCH (opposite geometry) -> this pass never judges it stale
    o = _staleable(kind="limit_entry", trigger=1532.0, anchor=1538.0, atr=64.1)
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1400.0) is False


def test_stale_geometry_failsafe_missing_or_nonfinite_inputs():
    o = _staleable(trigger=1532.0, anchor=1538.0, atr=64.1)
    assert _stale_geometry(o, swing_high=1892.39, swing_low=None) is False     # missing swing
    assert _stale_geometry(o, swing_high=1892.39, swing_low=float("nan")) is False
    assert _stale_geometry(o, swing_high=1892.39, swing_low=float("-inf")) is False
    bad = o.model_copy(update={"trigger_level": float("nan")})
    assert _stale_geometry(bad, swing_high=1892.39, swing_low=1503.6) is False  # bad level
    bad_anchor = o.model_copy(update={"anchor_swing": float("inf")})
    assert _stale_geometry(bad_anchor, swing_high=1892.39, swing_low=1503.6) is False


def test_stale_geometry_pct_floor_when_atr_zero():
    # atr 0 -> buffer falls back to 0.25% of level (1532*0.0025 = 3.83), line = 1528.17. swing_low
    # 1525 is below the line -> stale; swing_low 1530 (above the line) -> not stale.
    o = _staleable(trigger=1532.0, anchor=1538.0, atr=0.0)
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1525.0) is True
    assert _stale_geometry(o, swing_high=1892.39, swing_low=1530.0) is False


def test_stale_geometry_tighter_level_short_not_flagged():
    # a short deliberately placed ABOVE the 20-bar support (trigger 144, support 131 at arm) was
    # NEVER a breakdown-of-the-swing anchor: anchor 131 < line(143.5) -> not stale even though the
    # current swing_low (120) sits below the level. Protects valid tighter-level entries.
    o = _staleable(symbol="XRPUSDT", trigger=144.0, stop=150.0, atr=2.0, anchor=131.0)
    assert _stale_geometry(o, swing_high=200.0, swing_low=120.0) is False


def test_revalidate_triggers_partitions_stale_and_healthy():
    stale_short = _staleable(symbol="ETHUSDT", trigger=1532.0, anchor=1538.0, atr=64.1)
    healthy_short = _staleable(symbol="BTCUSDT", trigger=60000.0, stop=62000.0, atr=800.0,
                               anchor=60500.0)
    no_swing = _staleable(symbol="SOLUSDT", trigger=60.0, stop=62.0, atr=2.0, anchor=60.5)
    swings = {"ETHUSDT": (1892.39, 1503.6), "BTCUSDT": (66000.0, 61000.0)}  # SOL absent (feed gap)
    stale, healthy = revalidate_triggers([stale_short, healthy_short, no_swing], swings)
    assert [o.symbol for o in stale] == ["ETHUSDT"]
    assert {o.symbol for o in healthy} == {"BTCUSDT", "SOLUSDT"}  # no-swing kept (fail-safe)


def test_non_crypto_triggers_partitions_and_fails_closed():
    btc = _o(symbol="BTCUSDT")          # proven crypto -> tradeable
    xau = _o(symbol="XAUUSDT")          # proven non-crypto (tokenized gold) -> untradeable
    ghost = _o(symbol="GHOSTUSDT")      # ABSENT from the map -> fail-closed -> untradeable
    nullv = _o(symbol="NULLUSDT")       # mapped to None (unknown) -> fail-closed -> untradeable
    is_crypto = {"BTCUSDT": True, "XAUUSDT": False, "NULLUSDT": None}  # GHOST absent on purpose
    untradeable, tradeable = non_crypto_triggers([btc, xau, ghost, nullv], is_crypto)
    assert {o.symbol for o in tradeable} == {"BTCUSDT"}              # only PROVEN crypto kept
    assert {o.symbol for o in untradeable} == {"XAUUSDT", "GHOSTUSDT", "NULLUSDT"}  # fail-closed


def test_non_crypto_triggers_direction_agnostic():
    # SYMMETRY (Rule 5): classification touches only the SYMBOL, never the side.
    nc_long = _o(symbol="XAUUSDT", direction="long", trigger=4264.0, stop=4200.0)
    nc_short = _o(symbol="XAUUSDT", direction="short", trigger=4264.0, stop=4288.0)
    c_long = _o(symbol="ETHUSDT", direction="long")
    c_short = _o(symbol="ETHUSDT", direction="short")
    is_crypto = {"XAUUSDT": False, "ETHUSDT": True}
    untradeable, tradeable = non_crypto_triggers([nc_long, nc_short, c_long, c_short], is_crypto)
    assert {o.direction for o in untradeable} == {"long", "short"}  # BOTH sides of the stock retired
    assert {o.direction for o in tradeable} == {"long", "short"}    # BOTH sides of the coin kept


def test_stop_entry_wrong_side_of_mark_arm_guard():
    # cy80 fix: a SHORT breakdown stop_entry must sit BELOW the mark (room to break down); a LONG
    # breakout ABOVE. Placed on the wrong side, it would fire on the next close with no real break
    # (the BNB @611 short armed below a 605 mark, then fired off a 603.79 close).
    from futures_fund.pending_orders import stop_entry_wrong_side_of_mark
    # the actual cy80 BNB case: short stop_entry @611, mark 605 -> wrong-side (no breakdown room)
    assert stop_entry_wrong_side_of_mark(_o(direction="short", trigger=611.0, stop=616.0), 605.0)
    # a proper short breakdown trigger BELOW the mark -> ok
    assert not stop_entry_wrong_side_of_mark(_o(direction="short", trigger=597.0, stop=606.0), 605)
    # long breakout: trigger must be ABOVE the mark
    assert stop_entry_wrong_side_of_mark(_o(direction="long", trigger=95.0, stop=90.0), 100.0)
    assert not stop_entry_wrong_side_of_mark(_o(direction="long", trigger=105.0, stop=100.0), 100.0)
    # limit_entry is EXEMPT (rests on the far side by design)
    assert not stop_entry_wrong_side_of_mark(_o(direction="short", kind="limit_entry",
                                                trigger=611.0, stop=616.0), 605.0)
    # fail-safe: missing/non-finite mark -> not wrong-side (keep)
    assert not stop_entry_wrong_side_of_mark(_o(direction="short", trigger=611.0, stop=616.0), None)
