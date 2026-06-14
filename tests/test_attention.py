from __future__ import annotations

from datetime import UTC, datetime

import pytest

from futures_fund.attention import detect_spikes, is_spiking

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _digest(vol_24h: float, baseline: float) -> dict:
    """Minimal digest carrying just the two fields the spike scan reads."""
    return {
        "mention_volume_24h": vol_24h,
        "mention_volume_baseline": baseline,
    }


# --- is_spiking (single coin) ------------------------------------------------

def test_spikes_above_ratio_times_baseline() -> None:
    # baseline 6 -> ratio arm = 12; 24h volume 13 clears it (and 13 >= min_mentions 5).
    assert is_spiking(_digest(13, 6.0), ratio=2.0, min_mentions=5) is True


def test_not_spiking_below_ratio_times_baseline() -> None:
    # baseline 6 -> ratio arm = 12; 24h volume 10 is over min_mentions but under the ratio arm.
    assert is_spiking(_digest(10, 6.0), ratio=2.0, min_mentions=5) is False


def test_min_mentions_floor_blocks_low_baseline_coin() -> None:
    # baseline 0.5 -> ratio arm = 1.0, but the min_mentions=5 floor dominates: 3 < 5.
    assert is_spiking(_digest(3, 0.5), ratio=2.0, min_mentions=5) is False


def test_min_mentions_floor_satisfied_with_low_baseline() -> None:
    # baseline 0.5 -> ratio arm 1.0; floor 5. 24h volume 6 clears max(5, 1.0)=5.
    assert is_spiking(_digest(6, 0.5), ratio=2.0, min_mentions=5) is True


def test_threshold_is_max_of_floor_and_ratio_arm() -> None:
    # baseline 10 -> ratio arm = 20 dominates the floor of 5; 19 fails, 20 passes (>=).
    assert is_spiking(_digest(19, 10.0), ratio=2.0, min_mentions=5) is False
    assert is_spiking(_digest(20, 10.0), ratio=2.0, min_mentions=5) is True


def test_exact_floor_boundary_inclusive() -> None:
    # zero baseline -> ratio arm 0; floor 5. Exactly 5 mentions spikes (>=).
    assert is_spiking(_digest(5, 0.0), ratio=2.0, min_mentions=5) is True
    assert is_spiking(_digest(4, 0.0), ratio=2.0, min_mentions=5) is False


def test_missing_fields_treated_as_zero() -> None:
    # empty digest -> 0 volume vs floor 5 -> not spiking; never raises.
    assert is_spiking({}, ratio=2.0, min_mentions=5) is False


def test_garbage_fields_degrade_to_zero() -> None:
    assert is_spiking(
        {"mention_volume_24h": "lots", "mention_volume_baseline": None},
        ratio=2.0, min_mentions=5,
    ) is False


def test_custom_ratio() -> None:
    # ratio 3.0, baseline 4 -> ratio arm 12. 12 passes, 11 fails.
    assert is_spiking(_digest(12, 4.0), ratio=3.0, min_mentions=5) is True
    assert is_spiking(_digest(11, 4.0), ratio=3.0, min_mentions=5) is False


# --- detect_spikes (across coins) --------------------------------------------

def test_detect_spikes_picks_only_spiking_coins() -> None:
    digests = {
        "DOGE": _digest(20, 4.0),   # ratio arm 8, vol 20 -> spikes
        "BTC": _digest(10, 8.0),    # ratio arm 16, vol 10 -> no
        "SOL": _digest(3, 0.2),     # under floor -> no
    }
    assert detect_spikes(digests, NOW) == ["DOGE"]


def test_detect_spikes_sorted_unique() -> None:
    digests = {
        "doge": _digest(20, 1.0),
        "pepe": _digest(30, 1.0),
        "wif": _digest(50, 1.0),
    }
    assert detect_spikes(digests, NOW) == ["DOGE", "PEPE", "WIF"]


def test_detect_spikes_normalises_case() -> None:
    assert detect_spikes({"doge": _digest(20, 1.0)}, NOW) == ["DOGE"]


def test_detect_spikes_empty_input() -> None:
    assert detect_spikes({}, NOW) == []


def test_detect_spikes_none_spiking() -> None:
    digests = {"BTC": _digest(2, 5.0), "ETH": _digest(1, 0.1)}
    assert detect_spikes(digests, NOW) == []


def test_detect_spikes_skips_non_dict_values() -> None:
    digests = {"BTC": _digest(20, 1.0), "JUNK": None, "BAD": 42}
    assert detect_spikes(digests, NOW) == ["BTC"]


def test_detect_spikes_respects_custom_params() -> None:
    digests = {"BTC": _digest(15, 4.0)}
    # default ratio 2 -> arm 8 -> spikes; ratio 5 -> arm 20 -> no.
    assert detect_spikes(digests, NOW) == ["BTC"]
    assert detect_spikes(digests, NOW, ratio=5.0) == []


def test_detect_spikes_min_mentions_floor_respected() -> None:
    # low baseline coin with 4 mentions stays out at the default floor of 5,
    # but appears once the floor is lowered to 3.
    digests = {"PEPE": _digest(4, 0.5)}
    assert detect_spikes(digests, NOW) == []
    assert detect_spikes(digests, NOW, min_mentions=3) == ["PEPE"]


def test_detect_spikes_is_pure_no_mutation() -> None:
    digests = {"DOGE": _digest(20, 1.0)}
    before = dict(digests["DOGE"])
    detect_spikes(digests, NOW)
    assert digests["DOGE"] == before
    assert set(digests) == {"DOGE"}
