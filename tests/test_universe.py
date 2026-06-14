from __future__ import annotations

from datetime import UTC, datetime

from futures_fund.universe import build_universe

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC)


def _digest(vol_24h: float, baseline: float) -> dict:
    return {
        "mention_volume_24h": vol_24h,
        "mention_volume_baseline": baseline,
    }


def test_core_always_present_even_when_not_spiking() -> None:
    # no digests at all -> spiking empty, but core survives in core+all.
    u = build_universe(["BTC", "ETH"], {}, NOW)
    assert u["core"] == ["BTC", "ETH"]
    assert u["spiking"] == []
    assert u["all"] == ["BTC", "ETH"]


def test_spiking_coin_added_to_universe() -> None:
    digests = {"DOGE": _digest(20, 1.0)}
    u = build_universe(["BTC"], digests, NOW)
    assert u["core"] == ["BTC"]
    assert u["spiking"] == ["DOGE"]
    assert u["all"] == ["BTC", "DOGE"]


def test_union_has_no_duplicates_when_core_also_spikes() -> None:
    # BTC is core AND spiking: it must appear once, and only under core (not spiking).
    digests = {"BTC": _digest(30, 1.0), "DOGE": _digest(20, 1.0)}
    u = build_universe(["BTC"], digests, NOW)
    assert u["core"] == ["BTC"]
    assert u["spiking"] == ["DOGE"]      # BTC excluded from spiking-only
    assert u["all"] == ["BTC", "DOGE"]   # no dup
    assert u["all"].count("BTC") == 1


def test_non_spiking_digest_does_not_expand_universe() -> None:
    digests = {"SOL": _digest(2, 5.0)}   # ratio arm 10, vol 2 -> no spike
    u = build_universe(["BTC", "ETH"], digests, NOW)
    assert u["spiking"] == []
    assert u["all"] == ["BTC", "ETH"]


def test_all_is_sorted_unique_union() -> None:
    digests = {"WIF": _digest(50, 1.0), "PEPE": _digest(40, 1.0)}
    u = build_universe(["ETH", "BTC"], digests, NOW)
    assert u["core"] == ["BTC", "ETH"]            # sorted
    assert u["spiking"] == ["PEPE", "WIF"]        # sorted
    assert u["all"] == ["BTC", "ETH", "PEPE", "WIF"]


def test_case_normalised_across_core_and_digests() -> None:
    digests = {"doge": _digest(20, 1.0)}
    u = build_universe(["btc"], digests, NOW)
    assert u["core"] == ["BTC"]
    assert u["spiking"] == ["DOGE"]
    assert u["all"] == ["BTC", "DOGE"]


def test_duplicate_core_entries_deduped() -> None:
    u = build_universe(["BTC", "BTC", "btc"], {}, NOW)
    assert u["core"] == ["BTC"]
    assert u["all"] == ["BTC"]


def test_core_spiking_via_digest_kept_when_lowercase_match() -> None:
    # core "btc" and a spiking digest "BTC" are the same coin -> single entry, core wins.
    digests = {"BTC": _digest(30, 1.0)}
    u = build_universe(["btc"], digests, NOW)
    assert u["spiking"] == []
    assert u["all"] == ["BTC"]


def test_min_mentions_floor_respected_in_universe() -> None:
    digests = {"PEPE": _digest(4, 0.5)}  # 4 < floor 5 -> not spiking by default
    u_default = build_universe(["BTC"], digests, NOW)
    assert u_default["spiking"] == []
    u_low = build_universe(["BTC"], digests, NOW, min_mentions=3)
    assert u_low["spiking"] == ["PEPE"]


def test_custom_ratio_forwarded() -> None:
    digests = {"DOGE": _digest(15, 4.0)}  # arm 8 at ratio 2, arm 20 at ratio 5
    assert build_universe(["BTC"], digests, NOW)["spiking"] == ["DOGE"]
    assert build_universe(["BTC"], digests, NOW, ratio=5.0)["spiking"] == []


def test_pure_no_mutation_of_inputs() -> None:
    core = ["BTC"]
    digests = {"DOGE": _digest(20, 1.0)}
    build_universe(core, digests, NOW)
    assert core == ["BTC"]
    assert set(digests) == {"DOGE"}


def test_empty_core_returns_only_spiking() -> None:
    digests = {"DOGE": _digest(20, 1.0)}
    u = build_universe([], digests, NOW)
    assert u["core"] == []
    assert u["spiking"] == ["DOGE"]
    assert u["all"] == ["DOGE"]
