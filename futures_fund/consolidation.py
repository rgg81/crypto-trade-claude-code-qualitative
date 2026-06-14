from __future__ import annotations

from futures_fund.models import SizedTrade
from futures_fund.policy import cvar
from futures_fund.portfolio_risk import _corr, position_risk


def cvar_risk_multiplier(recent_returns: list[float], threshold: float = -0.05,
                         floor: float = 0.5) -> float:
    """1.0 in calm tails; `floor` when CVaR breaches `threshold` (portfolio-level de-risk)."""
    if not recent_returns:
        return 1.0
    return floor if cvar(recent_returns) < threshold else 1.0


def _scale(st: SizedTrade, factor: float) -> SizedTrade:
    return st.model_copy(update={
        "qty": st.qty * factor,
        "notional": st.notional * factor,
        "margin": st.margin * factor,
    })


def consolidate(
    approved: list[SizedTrade], equity: float, max_heat: float,
    cvar_mult: float = 1.0, min_risk_frac: float = 0.001,
) -> list[SizedTrade]:
    """Turn the per-symbol approved trades into a final book: apply the portfolio-level CVaR
    de-risk, scale the batch down to the gross-heat cap, then drop dust positions.

    Gross heat here is the conservative sum of per-trade risk (>= any single correlation
    cluster's heat), so no unsafe book slips through. Cluster-aware refinement (treating
    correlated trades as one) is available via portfolio_risk.cluster_heat for Phase B's PM."""
    trades = [_scale(t, cvar_mult) for t in approved] if cvar_mult != 1.0 else list(approved)

    def risk(t: SizedTrade) -> float:
        return position_risk(t.qty, t.proposal.entry, t.proposal.stop, equity, t.proposal.direction)

    total = sum(risk(t) for t in trades)
    if total > max_heat and total > 0:
        factor = max_heat / total
        trades = [_scale(t, factor) for t in trades]

    return [t for t in trades if risk(t) >= min_risk_frac]


def cluster_scale(
    new_trades: list[SizedTrade], held: list[dict], equity: float,
    corr, cluster_cap: float, threshold: float = 0.7, min_risk_frac: float = 0.001,
) -> list[SizedTrade]:
    """Correlated-as-one: scale down NEW sized trades so that no same-direction cluster
    (held ∪ new, pairwise correlation >= threshold, union-find) exceeds `cluster_cap` heat.
    Held positions are already open and are NEVER scaled — their heat is reserved inside the
    cluster's budget. Stops the desk piling correlated same-direction bets into one oversized
    directional position (three correlated shorts behaving as one). Dust is dropped."""
    if not new_trades:
        return list(new_trades)
    rows = []  # (symbol, direction, risk_frac, is_new, new_idx)
    for p in held:
        rows.append((p["symbol"], p["direction"],
                     position_risk(p["qty"], p["entry"], p["stop"], equity, p["direction"]),
                     False, -1))
    for i, t in enumerate(new_trades):
        rows.append((t.proposal.symbol, t.proposal.direction,
                     position_risk(t.qty, t.proposal.entry, t.proposal.stop, equity,
                                   t.proposal.direction), True, i))
    n = len(rows)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a in range(n):
        for b in range(a + 1, n):
            if rows[a][1] == rows[b][1] and _corr(corr, rows[a][0], rows[b][0]) >= threshold:
                parent[find(a)] = find(b)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)

    factor = [1.0] * len(new_trades)
    for members in groups.values():
        total = sum(rows[i][2] for i in members)
        if total > cluster_cap and total > 0:
            held_heat = sum(rows[i][2] for i in members if not rows[i][3])
            new_heat = total - held_heat
            allowed = max(0.0, cluster_cap - held_heat)
            f = (allowed / new_heat) if new_heat > 0 else 0.0
            for i in members:
                if rows[i][3]:
                    factor[rows[i][4]] = min(factor[rows[i][4]], f)

    scaled = [_scale(t, factor[i]) for i, t in enumerate(new_trades)]
    return [t for t in scaled
            if position_risk(t.qty, t.proposal.entry, t.proposal.stop, equity,
                             t.proposal.direction) >= min_risk_frac]
