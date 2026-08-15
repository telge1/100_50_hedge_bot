#!/usr/bin/env python3
"""CLI: historical MySQL 5m structure direction over a UTC time range (read-only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research.regime_scanner.trend_direction_at import TrendDirectionAtError  # noqa: E402
from research.regime_scanner.trend_direction_range import (  # noqa: E402
    default_run_dir,
    format_range_text,
    query_trend_direction_range,
    write_range_artifacts,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Query C3.4B 5m trend direction (BULLISH/BEARISH/UNCLEAR) for each "
            "decision time in [start, end] using one causal scanner pass."
        )
    )
    p.add_argument("--symbol", required=True, help="e.g. APTUSDT / aptusdt")
    p.add_argument("--start", required=True, help="ISO-8601 UTC start (inclusive decision)")
    p.add_argument("--end", required=True, help="ISO-8601 UTC end (inclusive decision)")
    p.add_argument("--step", default="5m", help="Decision step (currently 5m only)")
    p.add_argument("--exchange", default="bybit")
    p.add_argument("--timeframe", default="5m", help="Primary TF (must be 5m)")
    p.add_argument("--output", choices=("text", "csv", "json"), default="text")
    p.add_argument(
        "--output-file",
        default=None,
        help="Optional explicit output path (csv/json body). Artifacts still written to run dir unless set alone for csv/json.",
    )
    p.add_argument(
        "--transitions-only",
        action="store_true",
        help="Emit only rows where direction/event/reason/state changes",
    )
    p.add_argument(
        "--include-forward-returns",
        action="store_true",
        help="Reserved (not implemented in v1; EX_POST_EVALUATION deferred)",
    )
    p.add_argument(
        "--env-file",
        default="research/regime_scanner/.env.regime_db",
        help="Path to REGIME_DB_* env file",
    )
    p.add_argument("--warmup-bars", type=int, default=72)
    p.add_argument(
        "--artifacts-dir",
        default=None,
        help="Optional artifacts directory (default: results/trend_direction_range/run_<utc>)",
    )
    p.add_argument(
        "--no-artifacts",
        action="store_true",
        help="Skip writing default artifact folder (stdout only / --output-file)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.include_forward_returns:
        print(
            "NOTE: --include-forward-returns is not implemented in v1; ignored.",
            file=sys.stderr,
        )
    try:
        result = query_trend_direction_range(
            symbol=args.symbol,
            start=args.start,
            end=args.end,
            step=args.step,
            exchange=args.exchange,
            timeframe=args.timeframe,
            warmup_bars=int(args.warmup_bars),
            env_file=args.env_file,
            transitions_only=bool(args.transitions_only),
        )
    except TrendDirectionAtError as exc:
        payload = {
            "error": True,
            "reason": exc.reason,
            "message": exc.message,
            "symbol": args.symbol,
            "start": args.start,
            "end": args.end,
        }
        if args.output == "json":
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"ERROR [{exc.reason}]: {exc.message}", file=sys.stderr)
        if exc.reason in {
            "TIMESTAMP_BEFORE_DATA",
            "TIMESTAMP_AFTER_DATA",
            "NO_CLOSED_CANDLE",
            "LOOKAHEAD_VIOLATION",
            "INVALID_RANGE",
        }:
            return 2
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR [UNEXPECTED]: {exc}", file=sys.stderr)
        return 1

    paths: dict[str, str] | None = None
    if not args.no_artifacts:
        out_dir = Path(args.artifacts_dir) if args.artifacts_dir else default_run_dir()
        paths = write_range_artifacts(result, out_dir)

    if args.output_file:
        out_path = Path(args.output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if args.output == "json" or out_path.suffix.lower() == ".json":
            out_path.write_text(json.dumps(result.to_dict(), indent=2, default=str) + "\n")
        else:
            import pandas as pd
            from research.regime_scanner.trend_direction_range import TIMELINE_COLUMNS

            pd.DataFrame(result.output_rows(), columns=TIMELINE_COLUMNS).to_csv(out_path, index=False)

    if args.output == "json":
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
    elif args.output == "csv":
        import pandas as pd
        from research.regime_scanner.trend_direction_range import TIMELINE_COLUMNS

        print(pd.DataFrame(result.output_rows(), columns=TIMELINE_COLUMNS).to_csv(index=False), end="")
    else:
        print(format_range_text(result, paths=paths))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
