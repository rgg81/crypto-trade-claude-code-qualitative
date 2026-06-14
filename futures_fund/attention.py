from __future__ import annotations

from datetime import datetime


def _as_float(value: object, default: float = 0.0) -> float:
    """Coerce a digest field to float, tolerating None/str/missing — never raises.

    Digests are JSON on disk and hand-built in tests, so a field may be absent or carry a
    string. A non-coercible value degrades to `default` (treated as 'no signal') rather than
    blowing up the spike scan."""
    if value is None:
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def is_spiking(
    digest: dict,
    *,
    ratio: float = 2.0,
    min_mentions: int = 5,
) -> bool:
    """True iff a single coin's 24h mention volume clears the spike threshold.

    A coin spikes when ``mention_volume_24h >= max(min_mentions, ratio * mention_volume_baseline)``.
    The ``min_mentions`` floor stops a coin with a near-zero baseline (e.g. baseline 0.1) from
    "spiking" on a couple of stray mentions; the ``ratio`` arm requires a genuine multiple of the
    coin's own normal chatter. Tolerant of missing/garbage fields (treated as 0)."""
    vol_24h = _as_float(digest.get("mention_volume_24h"))
    baseline = _as_float(digest.get("mention_volume_baseline"))
    threshold = max(float(min_mentions), ratio * baseline)
    return vol_24h >= threshold


def detect_spikes(
    digests: dict[str, dict],
    now: datetime,
    ratio: float = 2.0,
    min_mentions: int = 5,
) -> list[str]:
    """Coins whose 24h mention volume is spiking, as a sorted list of base tickers.

    Pure and deterministic: scans each coin's digest and applies :func:`is_spiking`. `now` is
    accepted for interface symmetry with the rest of the desk (the spike comparison reads the
    digest's pre-aggregated 24h/baseline counts, which were computed as-of their own update time).
    Coin keys are normalised to upper-case base tickers; the result is sorted for stable output."""
    spiking: set[str] = set()
    for coin, digest in digests.items():
        if not isinstance(digest, dict):
            continue
        if is_spiking(digest, ratio=ratio, min_mentions=min_mentions):
            spiking.add(str(coin).upper())
    return sorted(spiking)
