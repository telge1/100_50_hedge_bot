#!/usr/bin/env python3
"""Run fractal cycle wave efficiency analysis on existing MySQL candles (APTUSDT)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_cycle_wave_analysis import SYMBOL_PRIMARY  # noqa: E402
from orderbook_analyse.fractal_cycle_wave_analysis.analysis import (  # noqa: E402
    run_symbol_analysis,
)
from orderbook_analyse.fractal_cycle_wave_analysis.export import write_results  # noqa: E402
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import (  # noqa: E402
    DEFAULT_ENV_FILE,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--symbol", default=SYMBOL_PRIMARY)
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_cycle_wave_analysis_apt",
    )
    args = p.parse_args(argv)

    if args.symbol != "APTUSDT":
        # DOGE intentionally skipped unless caller overrides; coverage must be complete.
        print(f"NOTE: running non-primary symbol {args.symbol}", flush=True)

    print(f"[run] symbol={args.symbol} env={args.env_file}", flush=True)
    payload = run_symbol_analysis(symbol=args.symbol, env_file=args.env_file)
    paths = write_results(payload, args.out_dir)
    decision = (payload.get("visibility") or {}).get("decision")
    print(f"[done] decision={decision}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
