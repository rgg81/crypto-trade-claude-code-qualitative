from __future__ import annotations

import pandas as pd

from futures_fund.config import Settings
from futures_fund.market_data import (
    FundingInfo,
    _filter_field,
    is_crypto_market,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
)
from futures_fund.models import MmrBracket, SymbolSpec


def build_ccxt(settings: Settings):
    """Construct a ccxt binanceusdm client. Imported lazily so the test suite never needs
    ccxt's network stack.

    - Paper (settings.live is False, the default): a PUBLIC mainnet client — real market data,
      no API keys, no orders. Binance has deprecated ccxt's futures testnet/sandbox, so paper
      trading uses real mainnet data with in-process simulated execution (never sandbox).
    - Live (settings.live is True): an authenticated mainnet client for real orders.
    """
    import ccxt

    config: dict = {"enableRateLimit": True}
    if settings.live:
        if not settings.exchange.api_key or not settings.exchange.api_secret:
            raise ValueError(
                "live=True requires BINANCE_KEY/BINANCE_SECRET; refusing to build a live client "
                "without authenticated credentials (would also fail leverage-tier / order calls)."
            )
        config["apiKey"] = settings.exchange.api_key
        config["secret"] = settings.exchange.api_secret
    return ccxt.binanceusdm(config)


def default_symbol_spec(market: dict) -> SymbolSpec:
    """Build a SymbolSpec from PUBLIC exchangeInfo only (no leverage tiers). Used in paper/
    keyless mode, where the authenticated leverage-tiers endpoint is unavailable. Uses a single
    conservative MMR bracket (5% maintenance, 20x cap) so the risk gate computes a deliberately
    cautious liquidation price; real per-tier MMR is used whenever keys are present (live)."""
    filters = (market.get("info") or {}).get("filters") or []
    tick = _filter_field(filters, "PRICE_FILTER", "tickSize")
    step = _filter_field(filters, "LOT_SIZE", "stepSize")
    mn = _filter_field(filters, "MIN_NOTIONAL", "notional")
    if tick is None:
        tick = float(market["precision"]["price"])
    if step is None:
        step = float(market["precision"]["amount"])
    if mn is None:
        mn = float((market.get("limits", {}).get("cost", {}) or {}).get("min") or 5.0)
    return SymbolSpec(
        symbol=market["id"], tick_size=float(tick), step_size=float(step), min_notional=float(mn),
        mmr_brackets=[MmrBracket(notional_floor=0.0, notional_cap=1e12, mmr=0.05,
                                 maint_amount=0.0, max_leverage=20.0)],
    )


class FuturesExchange:
    """Thin wrapper over a ccxt-like client. Inject a fake client in tests."""

    def __init__(self, client, keyless: bool = False):
        self.client = client
        # keyless: leverage tiers (an authenticated endpoint) are unavailable, so symbol_spec
        # falls back to a conservative default bracket. True for paper; False for live.
        self.keyless = keyless

    @classmethod
    def from_settings(cls, settings: Settings) -> FuturesExchange:
        ex = build_ccxt(settings)
        ex.load_markets()
        return cls(ex, keyless=not settings.live)

    def _raw_id(self, symbol: str) -> str:
        return self.client.market(symbol)["id"]

    def unified_for_raw(self, raw_id: str) -> str | None:
        """Map a stored raw exchange id (e.g. 'BTCUSDT') back to its ccxt unified symbol
        (e.g. 'BTC/USDT:USDT'). Used to fold carried positions into the working universe so a
        held symbol outside this cycle's Watcher picks is never stranded unaudited. None if the
        market is unknown."""
        by_id = getattr(self.client, "markets_by_id", None)
        if by_id and raw_id in by_id:
            m = by_id[raw_id]
            return (m[0] if isinstance(m, list) else m)["symbol"]
        for sym, mk in getattr(self.client, "markets", {}).items():
            if mk.get("id") == raw_id:
                return sym
        return None

    def is_crypto_raw(self, raw_id: str) -> bool:
        """CRYPTO-ONLY classifier for a RAW exchange id (e.g. 'XAUUSDT'): resolve raw -> unified ->
        ccxt market and classify with `is_crypto_market`. The desk trades cryptocurrencies only, so
        this backs the gate's non-crypto trigger purge. Unknown/unresolvable id or any lookup error
        -> False (FAIL-CLOSED: never assert crypto we cannot prove). Direction-agnostic."""
        uni = self.unified_for_raw(raw_id)
        if uni is None:
            return False
        try:
            return is_crypto_market(self.client.market(uni))
        except Exception:  # noqa: BLE001 — unknown market / ccxt error -> fail-closed (not crypto)
            return False

    def symbol_spec(self, symbol: str) -> SymbolSpec:
        market = self.client.market(symbol)
        if self.keyless:
            return default_symbol_spec(market)  # paper: no auth for leverage tiers
        tiers = self.client.fetch_leverage_tiers([symbol])[symbol]
        return parse_symbol_spec(market, tiers)

    def ohlcv(self, symbol: str, timeframe: str = "4h", limit: int = 500) -> pd.DataFrame:
        return parse_ohlcv(self.client.fetch_ohlcv(symbol, timeframe, None, limit))

    def funding(self, symbol: str) -> FundingInfo:
        fr = self.client.fetch_funding_rate(symbol)
        try:
            interval = self.client.fetch_funding_interval(symbol)
        except Exception:
            interval = None  # symbol uses default 8h, or endpoint unavailable
        return parse_funding(fr, interval)

    def open_interest_history(
        self, symbol: str, period: str = "4h", limit: int = 200
    ) -> pd.DataFrame:
        return parse_open_interest_history(
            self.client.fetch_open_interest_history(symbol, period, None, limit)
        )

    def long_short_ratio(self, symbol: str, period: str = "4h", limit: int = 200) -> pd.DataFrame:
        # implicit fapiData endpoint takes the RAW binance id, not the unified symbol
        raw = self.client.fapiDataGetGlobalLongShortAccountRatio(
            {"symbol": self._raw_id(symbol), "period": period, "limit": limit}
        )
        return parse_long_short_ratio(raw)

    def mark_price(self, symbol: str) -> float:
        return float(self.client.fetch_funding_rate(symbol)["markPrice"])
