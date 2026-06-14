from __future__ import annotations

import numpy as np

from futures_fund.journal import realized_total  # TRUE realized incl. partial banks (cy78 review)

PERIODS_PER_YEAR = 2190.0  # 4h cycles: 6/day * 365


def sharpe(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = arr.std(ddof=1)
    if sd == 0:
        return 0.0
    return float(arr.mean() / sd * np.sqrt(periods_per_year))


def trial_sharpe_std(return_streams: list[list[float]], min_obs: int = 5) -> float | None:
    """Cross-trial Sharpe dispersion (sigma_SR) for the Deflated Sharpe Ratio: the std of each
    trial's PER-PERIOD Sharpe. A 'trial' is a distinct strategy bet (e.g. each symbol the desk
    selected to trade from the screened universe). Returns None when there are < 2 trials each
    with >= min_obs observations — the caller then falls back to the single-strategy reduction
    (sigma_SR = the Sharpe's own standard error) rather than guessing from sparse data."""
    shrps = [sharpe(s, periods_per_year=1.0) for s in return_streams if len(s) >= min_obs]
    if len(shrps) < 2:
        return None
    return float(np.std(shrps, ddof=1))


def sortino(returns: list[float], periods_per_year: float = PERIODS_PER_YEAR) -> float:
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    # Downside deviation about the 0 target, as the RMS of the negative parts over ALL N
    # observations — NOT the sample std of the negative subset (which collapses to 0 for a single
    # loss or equal losses, spuriously reporting infinite Sortino despite real downside risk).
    dd = float(np.sqrt(np.mean(np.minimum(arr, 0.0) ** 2)))
    if dd == 0:
        # truly no negative returns: infinite Sortino if net positive, else 0 (like profit_factor)
        return float("inf") if arr.mean() > 0 else 0.0
    return float(arr.mean() / dd * np.sqrt(periods_per_year))


def max_drawdown(equity: list[float]) -> float:
    """Largest peak-to-trough decline as a positive fraction (0 if monotonic up / too short)."""
    if len(equity) < 2:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for e in equity:
        peak = max(peak, e)
        if peak > 0:
            mdd = max(mdd, (peak - e) / peak)
    return mdd


def calmar(annual_return: float, mdd: float) -> float:
    return annual_return / mdd if mdd > 0 else 0.0


def hit_rate(closed: list[dict]) -> float:
    if not closed:
        return 0.0
    wins = sum(1 for d in closed if realized_total(d) > 0)
    return wins / len(closed)


def profit_factor(closed: list[dict]) -> float:
    gains = sum(realized_total(d) for d in closed if realized_total(d) > 0)
    losses = -sum(realized_total(d) for d in closed if realized_total(d) < 0)
    if losses == 0:
        return float("inf") if gains > 0 else 0.0
    return gains / losses


def agent_attribution(closed: list[dict]) -> dict[str, dict]:
    """Per-agent realized PnL, trade count, and hit-rate. A trade credits every agent in its
    `contributing_agents` list (falls back to 'unknown')."""
    out: dict[str, dict] = {}
    for d in closed:
        agents = d.get("contributing_agents") or ["unknown"]
        for a in agents:
            rec = out.setdefault(a, {"pnl": 0.0, "count": 0, "wins": 0})
            tot = realized_total(d)
            rec["pnl"] += tot
            rec["count"] += 1
            rec["wins"] += 1 if tot > 0 else 0
    for rec in out.values():
        rec["hit_rate"] = rec["wins"] / rec["count"] if rec["count"] else 0.0
    return out
