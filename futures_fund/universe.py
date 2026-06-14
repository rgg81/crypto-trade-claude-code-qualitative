from __future__ import annotations

from datetime import datetime

from futures_fund.attention import detect_spikes


def build_universe(
    core: list[str],
    digests: dict[str, dict],
    now: datetime,
    ratio: float = 2.0,
    min_mentions: int = 5,
) -> dict[str, list[str]]:
    """Build the dynamic trading universe: the config watchlist plus attention-spiking coins.

    `core` is the static watchlist from config (base tickers, e.g. ["BTC", "ETH"]); `digests` maps
    coin -> digest dict. Returns three sorted, de-duplicated lists of upper-case base tickers:

    - ``core``    : the watchlist, normalised/de-duped, ALWAYS present (a core coin stays in the
                    universe even when it is not currently spiking).
    - ``spiking`` : coins detected as spiking by :func:`detect_spikes` that are NOT already core
                    (the dynamic add-ins driven by a surge in mention volume).
    - ``all``     : the sorted unique union of core + spiking.

    Pure and deterministic. `now`, `ratio`, `min_mentions` are forwarded to the spike detector."""
    core_set = {str(c).upper() for c in core}
    spike_set = set(detect_spikes(digests, now, ratio=ratio, min_mentions=min_mentions))
    spiking_only = spike_set - core_set
    return {
        "core": sorted(core_set),
        "spiking": sorted(spiking_only),
        "all": sorted(core_set | spike_set),
    }
