"""Apply a Reflector-decided lesson state change (qualitative desk).

`confirm` is STATISTICS-GATED: a candidate lesson only promotes to VALIDATED once it recurs enough
(the count threshold inside :func:`futures_fund.lessons.statistically_promote`) AND the desk's edge
is statistically supported. Below the gate the confirmation still COUNTS (recurrence accrues) but
the lesson stays CANDIDATE — exactly the base desk's discipline, with the dropped `scorecard` module
replaced by an in-CLI deterministic statistical-support p-value re-derived from the closed-trade
journal (the one-sided probability that the mean per-trade R-multiple is positive). `demote` /
`retire` step a lesson down unconditionally.

Forked from the base desk's promote_lesson_cli; the dropped `scorecard.build_scorecard` import is
replaced by :func:`statistical_support` here.

    uv run python scripts/promote_lesson_cli.py --id <lesson_id> --action confirm|demote|retire
"""
from __future__ import annotations

import argparse
import math
import sys

from futures_fund.journal import read_all_decisions
from futures_fund.lessons import demote_lesson, retire_lesson, statistically_promote
from futures_fund.metrics import sharpe

_STATE_DIR = "state"
_MEMORY_DIR = "memory"
# Min closed trades before any statistical claim is trusted — too few samples can't prove an edge,
# so promotion is refused (p-value 0.0) rather than guessed from noise.
_MIN_TRADES = 5


def _closed_r_multiples(memory_dir) -> list[float]:
    """Per-trade R-multiples of every CLOSED decision (the unit return stream the edge rests on).

    A trade with no recorded `r_multiple` (legacy/degenerate geometry) is skipped — the statistical
    support rests only on trades whose risk-normalised outcome is known."""
    out: list[float] = []
    for d in read_all_decisions(memory_dir):
        if d.get("realized_pnl") is None:
            continue
        rm = d.get("r_multiple")
        if isinstance(rm, (int, float)) and not isinstance(rm, bool) and math.isfinite(rm):
            out.append(float(rm))
    return out


def statistical_support(memory_dir) -> float:
    """Deterministic one-sided p-value that the desk's mean per-trade R-multiple is POSITIVE.

    Replaces the dropped `scorecard.dsr_pvalue`. Computed from the closed-trade R-multiple stream:
    the (annualisation-free) Sharpe ``mean/std`` scaled by ``sqrt(N)`` is the t-like statistic, and
    the normal CDF maps it to the probability the true mean exceeds 0. FAIL-SAFE: < `_MIN_TRADES`
    closed trades (or zero dispersion) -> 0.0, so a thin/degenerate record can NEVER clear the
    promotion gate. Pure given the journal."""
    rs = _closed_r_multiples(memory_dir)
    n = len(rs)
    if n < _MIN_TRADES:
        return 0.0
    # sharpe(..., periods_per_year=1.0) == mean/std (no annualisation); * sqrt(N) -> t-like stat.
    t = sharpe(rs, periods_per_year=1.0) * math.sqrt(n)
    # one-sided normal CDF: P(mean > 0) ≈ Phi(t)
    return 0.5 * (1.0 + math.erf(t / math.sqrt(2.0)))


def run(memory_dir, lesson_id: str, action: str) -> dict:
    """Apply `action` to the lesson. Returns ``{action, id, ok, dsr_pvalue?}``."""
    if action == "confirm":
        dsr = statistical_support(memory_dir)
        ok = statistically_promote(memory_dir, lesson_id, dsr_pvalue=dsr)
        return {"action": action, "id": lesson_id, "ok": ok, "dsr_pvalue": dsr}
    fn = {"demote": demote_lesson, "retire": retire_lesson}[action]
    ok = fn(memory_dir, lesson_id)
    return {"action": action, "id": lesson_id, "ok": ok}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Apply a lesson state change (confirm/demote/retire).")
    ap.add_argument("--id", required=True)
    ap.add_argument("--action", choices=["confirm", "demote", "retire"], required=True)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    args = ap.parse_args(argv)
    out = run(args.memory_dir, args.id, args.action)
    suffix = ""
    if "dsr_pvalue" in out:
        suffix = f" (statistical support p={out['dsr_pvalue']:.3f})"
    print(f"{out['action']} {out['id']}: {'ok' if out['ok'] else 'not found'}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
