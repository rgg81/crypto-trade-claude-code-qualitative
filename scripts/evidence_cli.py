"""Evidence assembler CLI for the Operation ORACLE decision loop.

For every coin in this cycle's ``universe.json`` it assembles one self-contained
:class:`~futures_fund.decision_io.EvidencePacket` — the coin's rolling decayed-sentiment digest,
its in-window content items, a source breakdown, and a NON-DIRECTIONAL price card (mark + ATR for
stop placement & sizing ONLY, never a signal). Output is persisted atomically to
``state/cycle/<N>/evidence.json`` as ``{COIN: EvidencePacket}``.

    uv run python scripts/evidence_cli.py --cycle N
    uv run python scripts/evidence_cli.py --cycle N --now 2026-06-13T12:00:00Z

The price card is fed by an INJECTED ``price_fn(coin) -> (mark, atr)`` (see
:func:`futures_fund.decision_io.assemble_evidence`). In PAPER production it wraps the keyless public
ccxt exchange + a Wilder-style ATR over 4h OHLCV (:func:`make_paper_price_fn`); in tests a fake
price_fn is injected so the whole thing runs OFFLINE. Either way it is FAIL-SOFT: a price fetch that
raises for one coin degrades that coin's price card to None/unavailable and NEVER aborts the cycle —
the directional (qualitative) evidence for every coin is assembled regardless.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime

import pandas as pd

from futures_fund.config import Settings, load_settings
from futures_fund.cycle_io import load_output, save_output
from futures_fund.decision_io import assemble_evidence, make_price_fn

_STATE_DIR = "state"
_CONTENT_DIR = "content"
_ATR_PERIOD = 14
# Sentiment evidence window: the trailing N hours of content folded into each packet. 96h (~4 days)
# matches decision_io's default — long enough that a slow news cycle still carries items, short
# enough to stay current.
_WINDOW_HOURS = 96.0


def _parse_now(raw: str | None) -> datetime:
    if raw is None:
        return datetime.now(UTC)
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def symbol_for_coin(coin: str) -> str:
    """Map a base ticker (e.g. ``BTC``) to its USD-M perp unified ccxt symbol (``BTC/USDT:USDT``).

    The desk's universe carries bare base tickers; the exchange/market-data stack speaks unified
    symbols. This is the single mapping used by the paper price_fn."""
    return f"{coin.upper()}/USDT:USDT"


def _atr_from_ohlcv(df: pd.DataFrame, period: int = _ATR_PERIOD) -> float:
    """House ATR = rolling mean of true range over `period` 4h candles (the desk's standard).

    True range = max(high-low, |high-prev_close|, |low-prev_close|). Returns 0.0 when there are
    too few candles for the window; the caller treats a non-positive/NaN ATR as 'unavailable'."""
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = tr.rolling(period).mean().iloc[-1]
    val = float(atr)
    return val if val == val and val > 0 else 0.0  # val != val filters NaN


def make_paper_price_fn(exchange, *, timeframe: str = "4h"):
    """Build the PAPER ``price_fn(coin) -> (mark, atr)`` over the keyless public ccxt exchange.

    Mark comes from ``exchange.mark_price`` and ATR from a house rolling-mean true-range over the
    coin's 4h OHLCV. Wrapped through :func:`make_price_fn` so the price card it feeds is plainly
    RISK PLUMBING. Any per-coin fetch error propagates to `assemble_evidence`, which catches it and
    degrades that one card — it must never crash the whole cycle."""

    def atr_for_coin(coin: str) -> float:
        return _atr_from_ohlcv(exchange.ohlcv(symbol_for_coin(coin), timeframe=timeframe))

    return make_price_fn(exchange, symbol_for_coin, atr_for_coin)


def build(
    state_dir,
    content_dir,
    cycle: int,
    now: datetime,
    *,
    price_fn,
    window_hours: float = _WINDOW_HOURS,
) -> dict[str, dict]:
    """Assemble + persist this cycle's evidence packets keyed by upper-case coin.

    Reads ``state/cycle/<cycle>/universe.json`` (the `all` working set), assembles one packet per
    coin via the INJECTED `price_fn`, and writes ``state/cycle/<cycle>/evidence.json``. Returns the
    serialized ``{COIN: packet-dict}`` map (also what is written). Fail-soft end to end: a price
    fetch raising for one coin only degrades that coin's price card."""
    universe = load_output(state_dir, cycle, "universe")
    coins = list(universe.get("all", []))
    packets = assemble_evidence(
        content_dir, coins, now, window_hours=window_hours, price_fn=price_fn
    )
    out = {coin: json.loads(pkt.model_dump_json()) for coin, pkt in packets.items()}
    save_output(state_dir, cycle, "evidence", out)
    return out


def _paper_price_fn(settings: Settings):
    """Construct the PAPER price_fn from settings (keyless public ccxt). Live env only."""
    from futures_fund.exchange import FuturesExchange

    exchange = FuturesExchange.from_settings(settings)
    return make_paper_price_fn(exchange, timeframe=settings.timeframe)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Assemble per-coin evidence packets for a cycle.")
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--content-dir", default=_CONTENT_DIR)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--now", default=None, help="ISO timestamp (UTC); defaults to now")
    args = ap.parse_args(argv)

    settings = load_settings(args.config)
    price_fn = _paper_price_fn(settings)
    out = build(
        args.state_dir,
        args.content_dir,
        args.cycle,
        _parse_now(args.now),
        price_fn=price_fn,
    )
    print(json.dumps({"cycle": args.cycle, "coins": sorted(out)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
