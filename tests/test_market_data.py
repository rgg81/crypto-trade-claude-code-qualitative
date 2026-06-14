from futures_fund.market_data import (
    FundingInfo,
    is_crypto_market,
    parse_funding,
    parse_long_short_ratio,
    parse_ohlcv,
    parse_open_interest_history,
    parse_symbol_spec,
    scan_universe,
)


def _coin(sym):  # a ccxt market dict for a single-coin crypto perp
    return {"symbol": sym, "info": {"underlyingType": "COIN", "contractType": "PERPETUAL"}}


class _TickerClient:
    # scan_universe cross-references client.markets for the crypto-only allowlist, so the fake
    # must carry COIN metadata for the symbols it expects to keep (BTC, DOGE).
    markets = {"BTC/USDT:USDT": _coin("BTC/USDT:USDT"), "DOGE/USDT:USDT": _coin("DOGE/USDT:USDT")}

    def fetch_tickers(self):
        return {
            "BTC/USDT:USDT": {"quoteVolume": 1e10, "percentage": 0.1, "last": 70000.0},
            "DOGE/USDT:USDT": {"quoteVolume": 5e8, "percentage": -2.0, "last": 0.1},
            "ETH/USDT:USD": {"quoteVolume": 9e9, "percentage": 0.0, "last": 2000.0},  # not perp
            "FOO/USDT:USDT": {"quoteVolume": 0, "percentage": 0, "last": 1.0},  # zero vol -> skip
            "BAR/USDT:USDT": {"quoteVolume": 1e9, "percentage": 5.0, "last": None},  # no price
        }


def test_scan_universe_ranks_usdt_perps_by_volume():
    rows = scan_universe(_TickerClient(), top_n=2)
    assert [r["symbol"] for r in rows] == ["BTC/USDT:USDT", "DOGE/USDT:USDT"]
    assert rows[0]["vol_24h_usd"] == 1e10 and rows[0]["chg_24h_pct"] == 0.1


def test_scan_universe_excludes_non_perp_and_zero_volume():
    syms = {r["symbol"] for r in scan_universe(_TickerClient(), top_n=10)}
    assert "ETH/USDT:USD" not in syms  # spot/quarterly, not a USDT perp
    assert "FOO/USDT:USDT" not in syms and "BAR/USDT:USDT" not in syms


def _market(sym, underlying, contract="TRADIFI_PERPETUAL"):
    return {"symbol": sym, "info": {"underlyingType": underlying, "contractType": contract}}


def test_is_crypto_market_allowlist():
    # KEEP only single-coin crypto perps
    assert is_crypto_market(_coin("BTC/USDT:USDT")) is True
    assert is_crypto_market(_coin("1000PEPE/USDT:USDT")) is True   # scaled base still a coin
    assert is_crypto_market(_coin("USDC/USDT:USDT")) is True       # stable base still a coin
    assert is_crypto_market({"info": {"underlyingType": "COIN"}}) is True  # missing ct but clearly COIN
    # EXCLUDE every TradFi class Binance now lists as USDT perps
    for ut in ("COMMODITY", "EQUITY", "KR_EQUITY", "PREMARKET", "INDEX"):
        assert is_crypto_market(_market("X/USDT:USDT", ut)) is False
    # EXCLUDE a COIN-settled dated future (not a perp the desk trades)
    assert is_crypto_market(_market("BTC/USDT:USDT", "COIN", "CURRENT_QUARTER")) is False
    # FAIL-CLOSED on missing / ambiguous metadata
    assert is_crypto_market(None) is False
    assert is_crypto_market({}) is False
    assert is_crypto_market({"info": {}}) is False  # no underlyingType -> can't prove crypto


def _coin_base(sym, base):  # a COIN-classified perp that also carries its base asset
    return {"symbol": sym, "base": base,
            "info": {"underlyingType": "COIN", "contractType": "PERPETUAL", "baseAsset": base}}


def test_is_crypto_market_excludes_commodity_proxy_coins():
    # PAXG (PAX Gold) and XAUT (Tether Gold) are classified underlyingType=='COIN' by Binance, but
    # are gold-backed PROXY tokens — economically commodities, not cryptocurrencies. The desk trades
    # crypto only, so they are EXCLUDED despite the COIN tag (user call: "remove this paxg").
    assert is_crypto_market(_coin_base("PAXG/USDT:USDT", "PAXG")) is False
    assert is_crypto_market(_coin_base("XAUT/USDT:USDT", "XAUT")) is False
    # case/whitespace robust + base read from info.baseAsset when the unified base is absent
    assert is_crypto_market({"symbol": "PAXG/USDT:USDT",
                             "info": {"underlyingType": "COIN", "baseAsset": "paxg"}}) is False
    # a genuine coin that merely carries a base is still KEPT
    assert is_crypto_market(_coin_base("BTC/USDT:USDT", "BTC")) is True
    assert is_crypto_market(_coin_base("ETH/USDT:USDT", "ETH")) is True


class _MixedClient:
    markets = {
        "BTC/USDT:USDT": _coin("BTC/USDT:USDT"),
        "DOGE/USDT:USDT": _coin("DOGE/USDT:USDT"),
        "XAU/USDT:USDT": _market("XAU/USDT:USDT", "COMMODITY"),   # tokenized gold
        "MU/USDT:USDT": _market("MU/USDT:USDT", "EQUITY"),        # tokenized equity
        "SPCX/USDT:USDT": _market("SPCX/USDT:USDT", "PREMARKET"),
        "DEFI/USDT:USDT": _market("DEFI/USDT:USDT", "INDEX", "PERPETUAL"),  # crypto BASKET, not a coin
        "SKHYNIX/USDT:USDT": _market("SKHYNIX/USDT:USDT", "KR_EQUITY"),
        "BTCQ/USDT:USDT": _market("BTCQ/USDT:USDT", "COIN", "CURRENT_QUARTER"),  # coin quarterly
        "PAXG/USDT:USDT": _coin_base("PAXG/USDT:USDT", "PAXG"),   # gold-proxy COIN -> excluded
        # GHOST/USDT:USDT is intentionally ABSENT from markets -> fail-closed
    }

    def fetch_tickers(self):
        v = 1e9
        return {s: {"quoteVolume": v, "percentage": 1.0, "last": 1.0}
                for s in list(self.markets) + ["GHOST/USDT:USDT"]}


def test_scan_universe_keeps_only_coin_perps_excludes_tradfi():
    syms = {r["symbol"] for r in scan_universe(_MixedClient(), top_n=20)}
    assert syms == {"BTC/USDT:USDT", "DOGE/USDT:USDT"}  # ONLY the COIN perps
    for x in ("XAU/USDT:USDT", "MU/USDT:USDT", "SPCX/USDT:USDT", "DEFI/USDT:USDT",
              "SKHYNIX/USDT:USDT", "BTCQ/USDT:USDT", "PAXG/USDT:USDT", "GHOST/USDT:USDT"):
        assert x not in syms  # TradFi / index / quarterly / gold-proxy / absent all excluded

MARKET = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT:USDT",
    "precision": {"price": 0.1, "amount": 0.001},
    "limits": {"amount": {"min": 0.001, "max": 1000.0}, "cost": {"min": 100.0}},
    "contractSize": 1.0,
    "info": {"filters": []},
}
TIERS = [
    {"tier": 1, "minNotional": 0, "maxNotional": 50000, "maintenanceMarginRate": 0.004,
     "maxLeverage": 125, "info": {"cum": "0"}},
    {"tier": 2, "minNotional": 50000, "maxNotional": 250000, "maintenanceMarginRate": 0.005,
     "maxLeverage": 100, "info": {"cum": "50"}},
]


def test_parse_symbol_spec_maps_precision_and_brackets():
    spec = parse_symbol_spec(MARKET, TIERS)
    assert spec.symbol == "BTCUSDT"
    assert spec.tick_size == 0.1
    assert spec.step_size == 0.001
    assert spec.min_notional == 100.0
    assert len(spec.mmr_brackets) == 2
    b1 = spec.mmr_brackets[1]
    assert (b1.notional_floor, b1.notional_cap, b1.mmr, b1.maint_amount, b1.max_leverage) == \
        (50000.0, 250000.0, 0.005, 50.0, 100.0)


def test_parse_ohlcv_to_sorted_utc_dataframe():
    rows = [[1780000000000, 100.0, 105.0, 99.0, 104.0, 12.0],
            [1779996400000, 98.0, 101.0, 97.0, 100.0, 8.0]]  # out of order on purpose
    df = parse_ohlcv(rows)
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["timestamp"].is_monotonic_increasing
    assert str(df["timestamp"].dt.tz) == "UTC"
    assert df.iloc[-1]["close"] == 104.0


def test_parse_funding_uses_interval_or_defaults_8h():
    fr = {"symbol": "BTC/USDT:USDT", "fundingRate": 0.0001, "fundingTimestamp": 1780041600000,
          "markPrice": 73676.1, "indexPrice": 73702.25}
    fi = parse_funding(fr, {"interval": "4h", "info": {"fundingIntervalHours": 4}})
    assert isinstance(fi, FundingInfo)
    assert fi.current_rate == 0.0001
    assert fi.interval_hours == 4.0
    assert fi.mark_price == 73676.1
    assert str(fi.next_funding_ts.tzinfo) == "UTC"
    # absent interval -> default 8h
    assert parse_funding(fr, None).interval_hours == 8.0


def test_parse_open_interest_history():
    rows = [{"timestamp": 1780000000000, "openInterestAmount": 1234.5, "openInterestValue": 9.0e7},
            {"timestamp": 1779996400000, "openInterestAmount": 1200.0, "openInterestValue": 8.7e7}]
    df = parse_open_interest_history(rows)
    assert list(df.columns) == ["timestamp", "oi_amount", "oi_value"]
    assert df["timestamp"].is_monotonic_increasing
    assert df.iloc[-1]["oi_amount"] == 1234.5


def test_parse_long_short_ratio_casts_strings():
    raw = [{"symbol": "BTCUSDT", "longShortRatio": "1.5", "longAccount": "0.6",
            "shortAccount": "0.4", "timestamp": "1780000000000"}]
    df = parse_long_short_ratio(raw)
    assert df.iloc[0]["long_short_ratio"] == 1.5
    assert df.iloc[0]["long_account"] == 0.6


def test_parse_open_interest_empty_returns_empty_df():
    df = parse_open_interest_history([])
    assert df.empty


def test_parse_symbol_spec_prefers_raw_filters_over_precision():
    # precision given as decimal-PLACES (8, 3) which would be wrong if used as tick/step;
    # the raw filters must win and yield correct sizes.
    market = {
        **MARKET,
        "precision": {"price": 8, "amount": 3},
        "info": {"filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001"},
            {"filterType": "MIN_NOTIONAL", "notional": "100"},
        ]},
    }
    spec = parse_symbol_spec(market, TIERS)
    assert spec.tick_size == 0.1
    assert spec.step_size == 0.001
    assert spec.min_notional == 100.0


def test_parse_long_short_ratio_skips_malformed_rows():
    raw = [
        {"symbol": "BTCUSDT", "longShortRatio": "1.5", "longAccount": "0.6",
         "shortAccount": "0.4", "timestamp": "1780000000000"},
        {"symbol": "BTCUSDT"},  # malformed: missing fields -> skipped, not fatal
    ]
    df = parse_long_short_ratio(raw)
    assert len(df) == 1
    assert df.iloc[0]["long_short_ratio"] == 1.5
