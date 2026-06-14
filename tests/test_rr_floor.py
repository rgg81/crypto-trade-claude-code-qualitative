import json

from futures_fund.rr_floor import (
    BAND,
    QUADRANTS,
    SEED,
    adapt_rr_floor,
    effective_rr_floor,
    load_rr_floor,
    save_rr_floor,
)


def test_seed_and_band_constants():
    assert SEED == 2.0 and BAND == (1.6, 2.5)
    assert set(QUADRANTS) == {"high_vol_trend", "low_vol_trend", "high_vol_range", "low_vol_range"}


def test_load_missing_returns_all_seed(tmp_path):
    state = load_rr_floor(tmp_path)
    assert all(state[q] == SEED for q in QUADRANTS) and state["updated_cycle"] == 0


def test_load_corrupt_or_partial_fails_safe(tmp_path):
    (tmp_path / "rr_floor.json").write_text("{ not json")
    assert all(load_rr_floor(tmp_path)[q] == SEED for q in QUADRANTS)
    (tmp_path / "rr_floor.json").write_text(json.dumps({"low_vol_range": 1.7}))
    s = load_rr_floor(tmp_path)
    assert s["low_vol_range"] == 1.7 and s["high_vol_trend"] == SEED   # missing keys -> SEED


def test_effective_clamps_to_band():
    assert effective_rr_floor("low_vol_range", {"low_vol_range": 1.4}) == 1.6   # below band
    assert effective_rr_floor("high_vol_trend", {"high_vol_trend": 3.0}) == 2.5  # above band
    assert effective_rr_floor("low_vol_trend", {"low_vol_trend": 1.9}) == 1.9    # in band
    assert effective_rr_floor("missing_q", {}) == SEED                          # unknown -> SEED


def test_save_then_load_roundtrip(tmp_path):
    save_rr_floor(tmp_path, {"high_vol_trend": 2.1, "low_vol_trend": 2.0,
                             "high_vol_range": 1.8, "low_vol_range": 1.7, "updated_cycle": 5})
    s = load_rr_floor(tmp_path)
    assert s["low_vol_range"] == 1.7 and s["updated_cycle"] == 5


def _seed_state():
    return {q: SEED for q in QUADRANTS} | {"updated_cycle": 0}


def test_adapt_loosens_when_vetoes_cost_winners():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_range": (7, 1)}, cycle_no=10)  # w=0.875
    assert new["low_vol_range"] == 1.95 and new["updated_cycle"] == 10
    assert any("low_vol_range" in c for c in changes)


def test_adapt_tightens_when_vetoes_save_losers():
    new, _ = adapt_rr_floor(_seed_state(), {"high_vol_trend": (2, 8)}, cycle_no=11)  # w=0.2
    assert new["high_vol_trend"] == 2.10


def test_adapt_deadband_no_change():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_trend": (5, 5)}, cycle_no=12)  # w=0.5
    assert new["low_vol_trend"] == SEED and changes == []


def test_adapt_requires_min_samples():
    new, changes = adapt_rr_floor(_seed_state(), {"low_vol_range": (7, 0)}, cycle_no=13)  # 7<8
    assert new["low_vol_range"] == SEED and changes == []


def test_adapt_clamps_at_bounds():
    st = _seed_state() | {"low_vol_range": 1.6}
    new, _ = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, cycle_no=14)   # 1.55 -> clamp 1.6
    assert new["low_vol_range"] == 1.6
    st2 = _seed_state() | {"high_vol_trend": 2.5}
    new2, _ = adapt_rr_floor(st2, {"high_vol_trend": (0, 8)}, cycle_no=15)  # 2.6 -> clamp 2.5
    assert new2["high_vol_trend"] == 2.5


def test_integration_floor_gates_then_admits():
    # seed gates a 1.7-RR trade; after a quadrant learns down to 1.6 over winning samples it admits.
    st = {q: SEED for q in QUADRANTS} | {"updated_cycle": 0}
    assert effective_rr_floor("low_vol_range", st) == 2.0           # seed: a 1.7 trade is vetoed
    for c in range(1, 12):                                          # 8/0 won each cycle -> loosen
        st, _ = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, c)
    assert effective_rr_floor("low_vol_range", st) == 1.6           # clamped at the hard floor
    assert effective_rr_floor("high_vol_trend", st) == 2.0          # other quadrants untouched


def test_hostile_rr_floor_file_clamps_up_to_hard_min(tmp_path):
    # END-TO-END: a hand-corrupt/hostile rr_floor.json can ONLY RAISE the floor — every quadrant a
    # caller reads via load_rr_floor->effective_rr_floor is clamped up to the 1.6 hard floor, never
    # below. (With test_gate_hard_min_wraps_corrupt_floor, the full file->gate chain holds.)
    import json
    (tmp_path / "rr_floor.json").write_text(
        json.dumps({"low_vol_range": 0.1, "high_vol_trend": -5.0,
                    "low_vol_trend": float("nan"), "high_vol_range": "junk"}))
    st = load_rr_floor(tmp_path)
    for q in QUADRANTS:
        assert effective_rr_floor(q, st) >= 1.6           # nothing below the hard floor
    assert effective_rr_floor("low_vol_range", st) == 1.6  # 0.1 -> 1.6
    assert effective_rr_floor("high_vol_trend", st) == 1.6  # -5.0 -> 1.6
    assert effective_rr_floor("low_vol_trend", st) == 2.0   # NaN -> SEED (load fail-safe)
    assert effective_rr_floor("high_vol_range", st) == 2.0  # "junk" -> SEED


def test_adapt_pins_at_floor_after_consecutive_pushes():
    # a quadrant already AT the 1.6 floor that keeps wanting looser (w>0.60) is PINNED; after
    # PIN_ALERT consecutive pushes a 'PINNED' advisory is surfaced (regime model / loop may be off).
    from futures_fund.rr_floor import PIN_ALERT
    st = _seed_state() | {"low_vol_range": 1.6}
    last = []
    for c in range(1, PIN_ALERT + 1):
        st, last = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, c)   # w=1.0, clamped at 1.6
    assert st["low_vol_range"] == 1.6 and st["pins"]["low_vol_range"] == PIN_ALERT
    assert any("PINNED" in c and "low_vol_range" in c for c in last)


def test_adapt_no_pin_while_still_moving():
    st = _seed_state()                                              # 2.0, not a bound
    st, changes = adapt_rr_floor(st, {"low_vol_range": (8, 0)}, 1)  # moves 2.0 -> 1.95
    assert st["low_vol_range"] == 1.95 and st.get("pins", {}).get("low_vol_range", 0) == 0
    assert not any("PINNED" in c for c in changes)


def test_adapt_pin_resets_on_deadband():
    st = _seed_state() | {"low_vol_range": 1.6, "pins": {"low_vol_range": 4}}
    st, _ = adapt_rr_floor(st, {"low_vol_range": (5, 5)}, 10)       # w=0.5 dead-band -> reset
    assert st.get("pins", {}).get("low_vol_range", 0) == 0
