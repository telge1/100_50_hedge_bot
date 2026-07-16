"""CLI wrapper for Phase C3.3B indicator-pattern discovery."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.regime_scanner.indicator_pattern_discovery_c3_3b import (
    DEFAULT_BASELINE_DIR,
    DEFAULT_OUT,
    run_c33b_audit,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase C3.3B indicator pattern discovery")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--load-start", default="2026-01-01")
    parser.add_argument("--load-end", default="2026-05-15")
    parser.add_argument("--analyze-start", default="2026-02-01")
    parser.add_argument("--analyze-end", default="2026-04-30")
    parser.add_argument("--discovery-end", default="2026-03-20")
    parser.add_argument("--min-pattern-events", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    args = parser.parse_args(argv)
    summary = run_c33b_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        discovery_end=args.discovery_end,
        min_pattern_events=args.min_pattern_events,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "n_events": summary["event_counts"]["total"],
                "n_candidates": summary["candidate_summary"]["n_candidates"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
