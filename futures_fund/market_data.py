from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
from pydantic import BaseModel, Field

from futures_fund.models import MmrBracket, SymbolSpec


class FundingInfo(BaseModel):
    symbol: str
    current_rate: float = Field(
        description="Current (last) funding rate, NOT a prediction "
        "(ccxt fundingRate == Binance lastFundingRate)."
    )
    next_funding_ts: datetime
    interval_hours: float
    mark_price: float
    index_price: float


def _filter_field(filters: list[dict], filter_type: str, field: str) -> float | None:
    for f in filters:
        if f.get("filterType") == filter_type and field in f:
            return float(f[field])
    return None


def parse_symbol_spec(market: dict, tiers: list[dict]) -> SymbolSpec:
    """ccxt market dict + leverage tiers -> SymbolSpec.

    Prefers Binance's authoritative exchangeInfo filters (PRICE_FILTER.tickSize,
    LOT_SIZE.stepSize, MIN_NOTIONAL.notional) as the source of truth; falls back to
    ccxt-unified precision/limits if filters are absent. This avoids silently treating a
    decimal-places precision count as a tick size if ccxt's precisionMode ever changes.
    """
    filters = (market.get("info") or {}).get("filters") or []
    tick = _filter_field(filters, "PRICE_FILTER", "tickSize")
    step = _filter_field(filters, "LOT_SIZE", "stepSize")
    min_notional = _filter_field(filters, "MIN_NOTIONAL", "notional")
    if tick is None:
        tick = float(market["precision"]["price"])
    if step is None:
        step = float(market["precision"]["amount"])
    if min_notional is None:
        min_notional = float(market["limits"]["cost"]["min"])
    brackets = [
        MmrBracket(
            notional_floor=float(t["minNotional"]),
            notional_cap=float(t["maxNotional"]),
            mmr=float(t["maintenanceMarginRate"]),
            maint_amount=float(t["info"]["cum"]),
            max_leverage=float(t["maxLeverage"]),
        )
        for t in tiers
    ]
    return SymbolSpec(
        symbol=market["id"],
        tick_size=tick,
        step_size=step,
        min_notional=min_notional,
        mmr_brackets=brackets,
    )


# Gold/commodity-PROXY tokens Binance tags underlyingType=='COIN' but which are economically a
# commodity (a metal-backed claim), NOT a cryptocurrency. The desk trades crypto only, so these are
# excluded despite the COIN tag (PAXG = PAX Gold, XAUT = Tether Gold). Keyed on the base asset.
_COMMODITY_PROXY_BASES = frozenset({"PAXG", "XAUT"})


def is_crypto_market(market: dict | None) -> bool:
    """CRYPTO-ONLY allowlist over a ccxt market dict (the one load_markets() populates). The desk
    trades cryptocurrencies ONLY. Binance now lists tokenized equities/commodities/metals/pre-IPO
    and crypto baskets as USDT perps (market.info.underlyingType in EQUITY / KR_EQUITY / COMMODITY
    / PREMARKET / INDEX); a CRYPTOCURRENCY is exactly underlyingType == 'COIN'. INDEX (BTCDOM/DEFI
    baskets) is excluded too — a basket/dominance index is not a single coin. Gold-backed PROXY
    coins (PAXG, XAUT — `_COMMODITY_PROXY_BASES`) are excluded as well: Binance tags them COIN but
    they are economically a commodity claim, not a cryptocurrency.

    FAIL-CLOSED on missing / ambiguous metadata: a market we cannot PROVE is a coin is excluded
    (a real coin is just re-scouted next cycle; a TradFi instrument must NEVER leak in). The
    secondary contractType reject (TRADIFI / dated-QUARTER) is belt-and-suspenders. Pure and
    DIRECTION-AGNOSTIC — classifies the instrument only, never side/size (long/short symmetric)."""
    if not market:
        return False
    info = market.get("info") or {}
    if info.get("underlyingType") != "COIN":   # positive allowlist (excludes TradFi + INDEX + None)
        return False
    ct = str(info.get("contractType") or "").upper()
    if "TRADIFI" in ct or "QUARTER" in ct:     # TradFi perp / COIN-dated future != crypto perp
        return False
    base = str(market.get("base") or info.get("baseAsset") or "").strip().upper()
    if base in _COMMODITY_PROXY_BASES:         # gold-proxy COIN (PAXG/XAUT) = commodity, not crypto
        return False
    return True


def scan_universe(client, top_n: int = 30) -> list[dict]:
    """Rank the live USD-M linear perp universe by 24h quote volume — the Watcher's scouting
    pool, recomputed every cycle so the universe rotates with the market. Public/keyless
    (one fetch_tickers call). Returns up to top_n rows: {symbol, last, chg_24h_pct, vol_24h_usd},
    most-liquid first. Skips non-USDT-perp symbols, anything with zero volume/price, and — the
    CRYPTO-ONLY gate — any market that is not a single-coin crypto perp (tokenized stocks /
    commodities / metals / pre-IPO / indices are excluded via `is_crypto_market`, cross-referencing
    `client.markets`; fail-closed if markets is unloaded so no non-crypto ever slips through)."""
    tickers = client.fetch_tickers()
    markets = getattr(client, "markets", None) or {}
    rows: list[dict] = []
    for sym, t in tickers.items():
        if not sym.endswith("/USDT:USDT"):
            continue
        if not is_crypto_market(markets.get(sym)):   # CRYPTO-ONLY: drop TradFi/index/unverifiable
            continue
        qv = t.get("quoteVolume") or 0.0
        last = t.get("last")
        if qv and last:
            rows.append({"symbol": sym, "last": float(last),
                         "chg_24h_pct": round(float(t.get("percentage") or 0.0), 2),
                         "vol_24h_usd": float(qv)})
    rows.sort(key=lambda r: r["vol_24h_usd"], reverse=True)
    return rows[:top_n]


def parse_ohlcv(rows: list[list]) -> pd.DataFrame:
    """ccxt OHLCV rows [[ts_ms,o,h,l,c,v], ...] -> sorted UTC-timestamped DataFrame."""
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return (
        df[["timestamp", "open", "high", "low", "close", "volume"]]
        .sort_values("timestamp")
        .reset_index(drop=True)
    )


def parse_funding(fr: dict, interval: dict | None = None) -> FundingInfo:
    interval_hours = 8.0
    if interval and (interval.get("info") or {}).get("fundingIntervalHours") is not None:
        interval_hours = float(interval["info"]["fundingIntervalHours"])
    return FundingInfo(
        symbol=fr["symbol"],
        current_rate=float(fr["fundingRate"]),
        next_funding_ts=datetime.fromtimestamp(fr["fundingTimestamp"] / 1000, tz=timezone.utc),  # noqa: UP017
        interval_hours=interval_hours,
        mark_price=float(fr["markPrice"]),
        index_price=float(fr["indexPrice"]),
    )


def parse_open_interest_history(rows: list[dict]) -> pd.DataFrame:
    cols = ["timestamp", "oi_amount", "oi_value"]
    recs = []
    for r in rows:
        try:
            recs.append({
                "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
                "oi_amount": float(r["openInterestAmount"]),
                "oi_value": (float(r["openInterestValue"])
                             if r.get("openInterestValue") is not None else float("nan")),
            })
        except (KeyError, ValueError, TypeError):
            continue  # skip a malformed row rather than dropping the whole batch
    if not recs:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)


def parse_long_short_ratio(raw_rows: list[dict]) -> pd.DataFrame:
    cols = ["timestamp", "long_short_ratio", "long_account", "short_account"]
    recs = []
    for r in raw_rows:
        try:
            recs.append({
                "timestamp": pd.to_datetime(int(r["timestamp"]), unit="ms", utc=True),
                "long_short_ratio": float(r["longShortRatio"]),
                "long_account": float(r["longAccount"]),
                "short_account": float(r["shortAccount"]),
            })
        except (KeyError, ValueError, TypeError):
            continue  # skip a malformed row rather than dropping the whole batch
    if not recs:
        return pd.DataFrame(columns=cols)
    return pd.DataFrame(recs).sort_values("timestamp").reset_index(drop=True)
