"""Standing self-audit CLI (Pillar 4 — AUDIT) for the qualitative sentiment desk.

    uv run python scripts/self_audit_cli.py
    uv run python scripts/self_audit_cli.py --state-dir state --content-dir content

Runs the desk's critical cross-module invariant panel (anti-martingale pacing, gate RR floor,
content-store integrity, auditor-gate presence on the latest completed cycle, and the
no-price-leak path sanity check) and prints PASS/FAIL per invariant. Exits 0 when ALL invariants
hold, 1 otherwise — a cheap deterministic standing check to run any cycle alongside (not instead
of) the full ``uv run pytest`` regression suite.
"""
from __future__ import annotations

import argparse
import sys

from futures_fund.self_audit import run_self_audit

_STATE_DIR = "state"
_CONTENT_DIR = "content"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the desk's standing invariant self-audit panel.")
    ap.add_argument("--state-dir", default=_STATE_DIR)
    ap.add_argument("--content-dir", default=_CONTENT_DIR)
    args = ap.parse_args(argv)

    res = run_self_audit(args.state_dir, args.content_dir)
    for c in res["checks"]:
        mark = "PASS" if c["ok"] else "FAIL"
        line = f"[{mark}] {c['name']}"
        if c["detail"]:
            line += f" — {c['detail']}"
        print(line)
    held = sum(c["ok"] for c in res["checks"])
    print(f"\nSELF-AUDIT: {'OK' if res['ok'] else 'FAILED'} "
          f"({held}/{len(res['checks'])} invariants hold)")
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
