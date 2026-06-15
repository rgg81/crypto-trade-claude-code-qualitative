"""Live mark-to-market equity for the open paper book.

Reads the persisted positions/account, fetches the CURRENT mark for each held symbol
from Binance (public, keyless), and prints unrealized PnL + mark equity. Read-only:
fetches marks only, never writes state. Used to surface a live equity line on the
fast crawler ticks (the 4h decision cycle is the only thing that mutates the book).
"""
from __future__ import annotations

import argparse
import json

from futures_fund.config import load_settings
from futures_fund.exchange import FuturesExchange
from futures_fund.state import load_account, load_positions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default="state")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    settings = load_settings(args.config)
    start = float(settings.account_size_usdt)
    account = load_account(args.state_dir, start)
    positions = load_positions(args.state_dir)

    rows = []
    total_unreal = 0.0
    ex = None
    if positions:
        try:
            ex = FuturesExchange.from_settings(settings)
        except Exception as e:  # noqa: BLE001 — surface, never crash the tick
            print(json.dumps({"error": f"exchange unavailable: {e}",
                              "balance": round(account.balance, 2)}))
            return

    for p in positions:
        mark = None
        note = ""
        try:
            uni = ex.unified_for_raw(p.symbol) or p.symbol
            mark = float(ex.mark_price(uni))
        except Exception as e:  # noqa: BLE001 — fail-soft per symbol
            note = f"mark unavailable: {e}"
        if mark and mark > 0:
            unreal = (p.entry - mark) * p.qty if p.direction == "short" else (mark - p.entry) * p.qty
            total_unreal += unreal
        else:
            unreal = 0.0
        rows.append({
            "symbol": p.symbol, "dir": p.direction, "entry": p.entry,
            "mark": mark, "unrealized": round(unreal, 2), "note": note,
        })

    equity = account.balance + total_unreal
    ret_pct = (equity - start) / start * 100.0 if start else 0.0
    print(json.dumps({
        "balance": round(account.balance, 2),
        "unrealized": round(total_unreal, 2),
        "equity_mark": round(equity, 2),
        "return_pct": round(ret_pct, 3),
        "n_positions": len(positions),
        "positions": rows,
    }))


if __name__ == "__main__":
    main()
