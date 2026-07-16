"""Phase C3.3A indicator-pattern discovery audit orchestrator (research-only).

This wrapper loads the 30m feature frame once, runs the discovery pipeline once,
and writes the audit bundle plus TradingView Pine review artifacts. It does not
modify production regime logic or any live bot configuration.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from research.regime_scanner.indicator_pattern_discovery import (
    DEFAULT_BASELINE_DIR,
    DEFAULT_OUT,
    run_ablation_on_candidates,
    run_audit as _run_audit,
    sensitivity_check,
)


def run_audit(**kwargs: Any) -> dict[str, Any]:
    return _run_audit(**kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase C3.3A indicator pattern discovery")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--timeframe", default="30m")
    parser.add_argument("--load-start", default="2026-01-01")
    parser.add_argument("--load-end", default="2026-05-15")
    parser.add_argument("--analyze-start", default="2026-02-01")
    parser.add_argument("--analyze-end", default="2026-04-30")
    parser.add_argument("--discovery-end", default="2026-03-20")
    parser.add_argument("--pre-bars", type=int, default=12)
    parser.add_argument("--post-bars", type=int, default=48)
    parser.add_argument("--horizons", nargs="+", type=int, default=[3, 6, 12, 24, 48, 96])
    parser.add_argument("--min-pattern-events", type=int, default=20)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("research/regime_scanner/results/phase_c3_3a_apt_pattern_discovery"),
    )
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE_DIR)
    args = parser.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        timeframe=args.timeframe,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        discovery_end=args.discovery_end,
        pre_bars=args.pre_bars,
        post_bars=args.post_bars,
        horizons=tuple(int(h) for h in args.horizons),
        min_pattern_events=args.min_pattern_events,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "n_events": summary["event_counts"]["total"],
                "n_candidates": len(summary["candidates"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
