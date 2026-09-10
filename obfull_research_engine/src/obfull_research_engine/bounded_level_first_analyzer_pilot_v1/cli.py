"""CLI for the bounded level-first pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BOUNDED_LEVEL_FIRST_ANALYZER_PILOT_V1")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--start", default="2026-09-06T19:00:00Z")
    parser.add_argument("--end", default="2026-09-06T23:00:00Z")
    parser.add_argument("--outcome-end", default="2026-09-06T23:30:00Z")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--precheck-only", action="store_true")
    parser.add_argument("--skip-local-ob-replay", action="store_true")
    args = parser.parse_args(argv)
    sys.path[:0] = [
        str(Path(__file__).resolve().parents[2]),
        str(Path("/home/telgenbuescher/projects/orderbook_analyse/src")),
        "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard",
        "/home/telgenbuescher/projects/trading_research_platform",
    ]
    from .runner import run_pilot

    result = run_pilot(
        symbol=args.symbol,
        start_z=args.start,
        end_z=args.end,
        outcome_end_z=args.outcome_end,
        resume=not args.no_resume,
        precheck_only=args.precheck_only,
        skip_local_ob_replay=args.skip_local_ob_replay,
    )
    printable = {k: result[k] for k in result if k not in {"manifest", "coverage"}}
    if args.precheck_only:
        printable = result
    print(json.dumps(printable, indent=2, default=str))
    if result.get("status") == "COMPLETE" or result.get("precheck_only"):
        return 0 if result.get("ok", True) or result.get("status") == "COMPLETE" else 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
