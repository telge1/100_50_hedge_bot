#!/usr/bin/env python3
"""Read-only OB200 v3 raw discovery CLI (V1 + V2 + V3)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from orderbook_analyse.ob200_v3_raw_discovery.runner import _parse_dt, run_discovery
from orderbook_analyse.ob200_v3_raw_discovery.runner_v2 import run_discovery_v2
from orderbook_analyse.ob200_v3_raw_discovery.v3.runner import run_discovery_v3


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw-root", type=Path, default=Path("data/orderbook_raw_shadow/ob200_v3"))
    p.add_argument("--symbols", default="BTCUSDT,DOGEUSDT")
    p.add_argument("--start", default="")
    p.add_argument("--end", default="")
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--v2-dir", type=Path, default=Path("results/ob200_v3_raw_discovery/btc_doge_v2"))
    p.add_argument("--audit", action="store_true")
    p.add_argument("--analyze", action="store_true")
    p.add_argument("--v2", action="store_true")
    p.add_argument("--v3", action="store_true")
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--sample-seconds", type=int, default=1)
    p.add_argument("--controls-per-event", type=int, default=3)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--qty-median-mult", type=float, default=3.0)
    args = p.parse_args(argv)

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    start = _parse_dt(args.start or None)
    end = _parse_dt(args.end or None)

    if args.v3:
        if start is None or end is None:
            raise SystemExit("--v3 requires --start and --end")
        manifest = run_discovery_v3(
            v2_dir=args.v2_dir,
            output_dir=args.output_dir,
            start=start,
            end=end,
            symbols=symbols,
            seed=args.seed,
        )
        keys = (
            "window_start_utc",
            "window_end_utc",
            "v2_complete_primary",
            "n_strict_full_strategy",
            "n_relaxed_full_strategy",
            "n_entry_candidates",
            "n_controls",
        )
    elif args.v2:
        manifest = run_discovery_v2(
            raw_root=args.raw_root,
            symbols=symbols,
            output_dir=args.output_dir,
            start=start,
            end=end,
            max_files=args.max_files or None,
            sample_seconds=args.sample_seconds,
            seed=args.seed,
            qty_median_mult=args.qty_median_mult,
        )
        keys = (
            "window_start_utc",
            "window_end_utc",
            "n_lifecycles",
            "n_primary_chains_v2",
            "n_complete_primary_v2",
        )
    else:
        if not args.audit and not args.analyze:
            args.audit = True
            args.analyze = True
        manifest = run_discovery(
            raw_root=args.raw_root,
            symbols=symbols,
            output_dir=args.output_dir,
            start=start,
            end=end,
            do_audit=args.audit,
            do_analyze=args.analyze,
            max_files=args.max_files or None,
            sample_seconds=args.sample_seconds,
            controls_per_event=args.controls_per_event,
            seed=args.seed,
            qty_median_mult=args.qty_median_mult,
        )
        keys = ("window_start_utc", "window_end_utc", "n_samples", "n_wall_events", "n_chains")

    print(json.dumps({k: manifest[k] for k in keys if k in manifest}, indent=2))
    print(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
