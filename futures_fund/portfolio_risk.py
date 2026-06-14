from __future__ import annotations

from collections.abc import Mapping


def position_risk(qty: float, entry: float, stop: float, equity: float,
                  direction: str | None = None) -> float:
    """Per-trade DOWNSIDE risk as a fraction of equity (loss if stopped out). A profit-locked
    stop — above entry for a long, below entry for a short — has NO downside, so it returns 0
    (it cannot consume heat budget). Without `direction` it falls back to the absolute stop
    distance (legacy; correct only for loss-side stops)."""
    if equity <= 0:
        return 0.0
    if direction == "long":
        loss = max(0.0, entry - stop)
    elif direction == "short":
        loss = max(0.0, stop - entry)
    else:
        loss = abs(entry - stop)
    return abs(qty) * loss / equity


def portfolio_heat(positions: list[dict], equity: float) -> float:
    """Sum of per-trade risks across all open positions, as a fraction of equity."""
    return sum(position_risk(p["qty"], p["entry"], p["stop"], equity, p.get("direction"))
               for p in positions)


def _corr(corr: Mapping[tuple[str, str], float], a: str, b: str) -> float:
    if (a, b) in corr:
        return corr[(a, b)]
    if (b, a) in corr:
        return corr[(b, a)]
    return 0.0


def cluster_heat(
    positions: list[dict], equity: float,
    corr: Mapping[tuple[str, str], float], threshold: float = 0.7,
) -> dict[int, float]:
    """Group same-direction positions whose pairwise correlation >= threshold (union-find),
    and return {cluster_id: combined_heat_fraction}.

    A cluster's combined heat is what the heat cap should be applied to, because correlated
    same-direction exposure behaves as one position under stress.
    """
    n = len(positions)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for i in range(n):
        for j in range(i + 1, n):
            pi, pj = positions[i], positions[j]
            if pi.get("direction") == pj.get("direction"):
                if _corr(corr, pi["symbol"], pj["symbol"]) >= threshold:
                    union(i, j)

    out: dict[int, float] = {}
    for idx, p in enumerate(positions):
        root = find(idx)
        out[root] = out.get(root, 0.0) + position_risk(p["qty"], p["entry"], p["stop"], equity,
                                                       p.get("direction"))
    return out
