"""The per-cycle DECISION-QA CLI — the SELF-IMPROVEMENT loop's observational stage.

    uv run python scripts/decision_qa_cli.py --cycle N
    uv run python scripts/decision_qa_cli.py --cycle N --state-dir state --content-dir content \
        --memory-dir memory

Runs AFTER the auditor each cycle (it is observational — it never gates a trade and never touches
the Auditor's verdict). It:

  1. re-derives the per-cycle :class:`~futures_fund.decision_qa.DecisionQA` from the cycle's
     artifacts via :func:`~futures_fund.decision_qa.analyze_cycle` (every citation re-resolved
     against the content store, ground truth);
  2. APPENDS that snapshot as ONE JSON line to ``memory/decision-qa.jsonl`` (the rolling QA log);
  3. folds it into ``memory/agent_reliability.json`` via :func:`update_reliability`;
  4. prints a one-line summary (per-lane hallucinated-citation counts + current reliability).

FAIL-SOFT end to end: a missing/torn artifact yields a 0/None metric (never a crash, never a halt),
so this stage can run on any cycle — even a partial one — without breaking the ladder.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from futures_fund.decision_qa import LANES, DecisionQA, analyze_cycle, update_reliability

_STATE_DIR = "state"
_CONTENT_DIR = "content"
_MEMORY_DIR = "memory"


def _append_jsonl(path: Path, qa: DecisionQA) -> None:
    """APPEND the QA snapshot as one line to ``memory/decision-qa.jsonl`` (the rolling QA log).

    Self-healing against a torn trailing line (the content_store append pattern): if the file does
    not already end in a newline, prepend one so a previous crash-truncated fragment is left as its
    own (garbage, tolerated) line rather than concatenated onto the new record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = qa.model_dump_json() + "\n"
    if path.exists() and path.stat().st_size > 0:
        with path.open("rb") as fh:
            fh.seek(-1, 2)
            if fh.read(1) != b"\n":
                line = "\n" + line
    with path.open("a") as fh:
        fh.write(line)


def run(state_dir, content_dir, memory_dir, cycle: int, alpha: float = 0.3) -> DecisionQA:
    """Analyze the cycle, append the snapshot to the JSONL log, update reliability, return QA."""
    qa = analyze_cycle(state_dir, content_dir, cycle)
    _append_jsonl(Path(memory_dir) / "decision-qa.jsonl", qa)
    update_reliability(memory_dir, qa, alpha=alpha)
    return qa


def _summary_line(qa: DecisionQA, reliability: dict) -> str:
    parts = []
    for lane in LANES:
        ls = qa.lanes.get(lane)
        halluc = ls.hallucinated_citations if ls else 0
        rel = reliability.get(lane, {}).get("reliability")
        rel_s = f"{rel:.3f}" if isinstance(rel, (int, float)) else "n/a"
        parts.append(f"{lane}:halluc={halluc} rel={rel_s}")
    aud = "n/a" if qa.auditor_passed is None else ("PASS" if qa.auditor_passed else "FAIL")
    return (
        f"DECISION-QA cycle {qa.cycle}: " + " | ".join(parts)
        + f" || auditor={aud} advisories={qa.n_advisories} blocked={qa.n_blocked_proposals}"
        + f" redundancy={qa.lane_redundancy_mean:.3f}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run the per-cycle DECISION-QA engine (self-improvement subsystem)."
    )
    ap.add_argument("--cycle", type=int, required=True)
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--content-dir", default=_CONTENT_DIR)
    ap.add_argument("--memory-dir", default=_MEMORY_DIR)
    ap.add_argument("--alpha", type=float, default=0.3, help="EWMA smoothing factor (0..1)")
    args = ap.parse_args(argv)

    qa = run(args.state_dir, args.content_dir, args.memory_dir, args.cycle, alpha=args.alpha)
    # re-read the persisted reliability for the print (update_reliability already wrote it).
    from futures_fund.decision_qa import _load_json  # local import: internal helper, CLI-only use

    reliability = _load_json(Path(args.memory_dir) / "agent_reliability.json") or {}
    print(_summary_line(qa, reliability if isinstance(reliability, dict) else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
