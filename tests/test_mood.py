from __future__ import annotations

import pytest
from pydantic import ValidationError

from futures_fund.models import (
    CrowdMood,
    PortfolioHealth,
    RegimeState,
    mood_to_regime,
)
from futures_fund.policy import caps_for

# Expected low-dispersion mapping: mood -> (quadrant, trend_direction)
_EXPECTED = {
    "euphoric": ("high_vol_range", "up"),
    "greedy": ("high_vol_trend", "up"),
    "neutral": ("low_vol_range", "neutral"),
    "fearful": ("high_vol_trend", "down"),
    "capitulation": ("high_vol_range", "down"),
}


@pytest.mark.parametrize("mood,expected", _EXPECTED.items())
def test_mood_maps_to_expected_quadrant(mood, expected):
    exp_quadrant, exp_trend = expected
    rs = mood_to_regime(mood, dispersion=0.0)
    assert rs.quadrant == exp_quadrant
    assert rs.trend_direction == exp_trend


@pytest.mark.parametrize("mood", list(_EXPECTED))
def test_low_dispersion_boundary_does_not_bump(mood):
    # Just below the 0.6 threshold keeps the base quadrant.
    rs = mood_to_regime(mood, dispersion=0.59)
    assert rs.quadrant == _EXPECTED[mood][0]


@pytest.mark.parametrize("mood,expected", _EXPECTED.items())
def test_high_dispersion_bumps_to_transition(mood, expected):
    _, exp_trend = expected
    rs = mood_to_regime(mood, dispersion=0.6)
    assert rs.quadrant == "transition"
    # Trend direction is preserved even when bumped to the cautious quadrant.
    assert rs.trend_direction == exp_trend
    # Above the threshold too.
    assert mood_to_regime(mood, dispersion=1.0).quadrant == "transition"


@pytest.mark.parametrize("mood", list(_EXPECTED))
def test_returned_regime_state_is_valid(mood):
    rs = mood_to_regime(mood, dispersion=0.3)
    assert isinstance(rs, RegimeState)
    # Round-trips through pydantic validation cleanly.
    RegimeState.model_validate(rs.model_dump())


def test_mood_to_regime_feeds_caps_for():
    # The mapping must produce a RegimeState the unchanged caps machinery accepts.
    health = PortfolioHealth(equity=10_000.0, peak_equity=10_000.0)
    rs_neutral = mood_to_regime("neutral", dispersion=0.0)
    rs_euphoric = mood_to_regime("euphoric", dispersion=0.0)
    rs_split = mood_to_regime("euphoric", dispersion=0.9)

    caps_neutral = caps_for(rs_neutral, health)
    caps_euphoric = caps_for(rs_euphoric, health)
    caps_split = caps_for(rs_split, health)

    # Extremes are tighter than a calm neutral crowd.
    assert caps_euphoric.max_leverage <= caps_neutral.max_leverage
    assert caps_euphoric.max_heat <= caps_neutral.max_heat
    # A maximally split crowd lands in the most cautious posture (transition -> reduce bias).
    assert caps_split.bias == "reduce"
    assert caps_split.max_heat <= caps_euphoric.max_heat


def test_crowd_mood_dispersion_bounds():
    m = CrowdMood(mood="greedy", dispersion=0.5, rationale="mixed signals")
    assert m.dispersion == 0.5
    assert m.rationale == "mixed signals"
    assert CrowdMood(mood="fearful", dispersion=0.0).rationale == ""
    with pytest.raises(ValidationError):
        CrowdMood(mood="greedy", dispersion=1.5)
    with pytest.raises(ValidationError):
        CrowdMood(mood="greedy", dispersion=-0.1)


def test_crowd_mood_rejects_bad_mood():
    with pytest.raises(ValidationError):
        CrowdMood(mood="elated", dispersion=0.2)
