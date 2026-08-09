#!/usr/bin/env python3
"""Causal paper/forward runner for frozen wave-fade cluster strategy v1."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_forward_paper import (  # noqa: E402
    DEFAULT_OUT_DIR,
    DEFAULT_PAPER_START,
)
from orderbook_analyse.fractal_wave_fade_forward_paper.runner import (  # noqa: E402
    run_once,
    run_parity_gate,
    run_replay,
    status,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    p.add_argument("--paper-start", default=DEFAULT_PAPER_START)
    p.add_argument("--fee", type=float, default=0.11)
    p.add_argument("--no-conflict-exit", action="store_true")
    p.add_argument("--once", action="store_true", help="Process new DB candles (FORWARD)")
    p.add_argument(
        "--replay-from",
        nargs="?",
        const=DEFAULT_PAPER_START,
        default=None,
        help="Technical REPLAY from PAPER_START (or given ISO ts)",
    )
    p.add_argument("--status", action="store_true")
    p.add_argument("--parity", action="store_true", help="Parity gate vs backtest (required before --once)")
    p.add_argument("--parity-start", default="2024-01-01T00:00:00+00:00")
    p.add_argument("--parity-end", default="2024-04-01T00:00:00+00:00")
    args = p.parse_args(argv)

    conflict = not args.no_conflict_exit

    if args.status:
        print(json.dumps(status(args.out_dir), indent=2, default=str))
        return 0

    if args.parity:
        report = run_parity_gate(
            args.out_dir, window_start=args.parity_start, window_end=args.parity_end
        )
        print(json.dumps(report, indent=2, default=str))
        return 0 if report.get("status") == "PAPER_RUNNER_MATCHES_BACKTEST" else 2

    if args.replay_from is not None:
        start = args.replay_from or args.paper_start
        res = run_replay(
            args.out_dir,
            paper_start=start,
            conflict_exit=conflict,
            fee_pct=args.fee,
        )
        print(json.dumps({"mode": "REPLAY", "coverage_flags": res["coverage_flags"], "summary": res["summary"]}, indent=2, default=str))
        return 0

    if args.once:
        res = run_once(
            args.out_dir,
            paper_start=args.paper_start,
            conflict_exit=conflict,
            fee_pct=args.fee,
        )
        print(json.dumps(res if res.get("blocked") else {"mode": "FORWARD", "coverage_flags": res.get("coverage_flags"), "data_stale": res.get("data_stale"), "summary": res.get("summary")}, indent=2, default=str))
        if res.get("blocked"):
            return 3
        if res.get("data_stale"):
            return 4
        return 0

    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
