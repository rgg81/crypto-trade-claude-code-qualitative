from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class ExchangeSettings(BaseModel):
    testnet: bool = True
    key_env: str = "BINANCE_KEY"
    secret_env: str = "BINANCE_SECRET"

    @property
    def api_key(self) -> str | None:
        return os.environ.get(self.key_env)

    @property
    def api_secret(self) -> str | None:
        return os.environ.get(self.secret_env)


class DataSettings(BaseModel):
    # Keyless crypto-news RSS feeds (each degrades independently; a dead/blocked source is skipped).
    # Broadened beyond coindesk+cointelegraph so the News analyst sees more of the tape.
    news_rss_sources: list[str] = Field(default_factory=lambda: [
        "https://www.coindesk.com/arc/outboundfeeds/rss/?outputType=xml",
        "https://cointelegraph.com/rss",
        "https://decrypt.co/feed",
        "https://www.cryptoslate.com/feed/",
        "https://bitcoinmagazine.com/feed",
        "https://cryptopotato.com/feed/",
    ])
    # Keyless reddit social-sentiment scrape (public /hot.json; degrades to empty if blocked).
    reddit_subreddits: list[str] = Field(
        default_factory=lambda: ["CryptoCurrency", "CryptoMarkets"])
    fred_key_env: str = "FRED_API_KEY"
    fred_series: list[str] = Field(
        default_factory=lambda: ["DTWEXBGS", "DGS10", "FEDFUNDS", "CPIAUCSL"]
    )
    archive_dir: str = "state/archive"

    @property
    def fred_api_key(self) -> str | None:
        return os.environ.get(self.fred_key_env)


class SourcesSettings(BaseModel):
    """Per-source crawl wiring for the qualitative desk.

    ``enabled`` is the list of source-adapter names the crawler runs (it maps onto the registry
    filter consumed by :func:`futures_fund.sources.enabled_adapters`). The remaining lists are the
    per-source upstream endpoints — RSS feeds, reddit subs, Nitter/Telegram mirrors, YouTube
    channels — that the adapters read defensively. Anything absent falls back to the adapter's own
    keyless defaults, so a sparse config still crawls. Extra keys are ignored (forward-compatible).
    """

    model_config = {"extra": "ignore"}

    enabled: list[str] = Field(default_factory=lambda: [
        "rss", "reddit", "nitter", "stocktwits", "telegram", "youtube", "forums",
    ])
    feeds: list[str] = Field(default_factory=list)
    subs: list[str] = Field(default_factory=list)
    mirrors: list[str] = Field(default_factory=list)
    channels: list[str] = Field(default_factory=list)


class QualitativeSettings(BaseModel):
    """Config for the qualitative sentiment desk (the crawler-loop + decision funnel).

    ``core_watchlist`` is the static set of liquid majors that ALWAYS sit in the working universe
    (base tickers, e.g. ``["BTC", "ETH"]``); the crawler additionally adds attention-spiking coins
    on top of it. ``crawl_interval_min`` is the crawler grid; the spike thresholds tune
    :func:`futures_fund.attention.detect_spikes`."""

    model_config = {"extra": "ignore"}

    core_watchlist: list[str] = Field(default_factory=lambda: [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC",
    ])
    crawl_interval_min: int = 15
    spike_ratio: float = 2.0
    spike_min_mentions: int = 5


class Settings(BaseModel):
    account_size_usdt: float = 10_000.0
    timeframe: str = "4h"
    symbol_count: int = 10
    symbols: list[str] = Field(default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"])
    deep_model: str = "opus"
    quick_model: str = "haiku"
    verdict_horizon_weeks: int = 8
    live: bool = False  # MUST be explicitly enabled; live also requires a 'graduated' verdict
    exchange: ExchangeSettings = Field(default_factory=ExchangeSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    qualitative: QualitativeSettings = Field(default_factory=QualitativeSettings)
    sources: SourcesSettings = Field(default_factory=SourcesSettings)


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ WITHOUT overriding existing env
    vars. Returns the parsed dict; no-op if the file is absent. So that secrets placed in
    .env (gitignored) are actually available to the cycle, which reads keys from os.environ."""
    p = Path(path)
    loaded: dict[str, str] = {}
    if not p.exists():
        return loaded
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if not k:
            continue
        loaded[k] = v
        os.environ.setdefault(k, v)  # real env wins over the file
    return loaded


def load_settings(path: str | Path = "config.yaml") -> Settings:
    """Load non-secret config from YAML (defaults if file absent). Secrets come from env;
    a .env file beside the config is auto-loaded into the environment first."""
    p = Path(path)
    load_env_file(p.parent / ".env")
    raw = yaml.safe_load(p.read_text()) if p.exists() else {}
    return Settings(**(raw or {}))
