#!/usr/bin/env python3
"""CLI for L2 Wall Attack Pattern Discovery V1."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from orderbook_analyse.l2_wall_attack_discovery.runner import run_wall_attack_discovery


def _parse_dt(raw: str) -> datetime:
    return datetime.fromisoformat(raw.strip().replace("Z", "+00:00")).astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw-root", type=Path, default=Path("data/orderbook_raw_shadow/ob200_v3"))
    p.add_argument("--symbols", default="BTCUSDT,DOGEUSDT")
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--audit", action="store_true", help="accepted for CLI parity; replay always audits")
    p.add_argument("--analyze", action="store_true", default=True)
    p.add_argument("--sample-ms", type=int, default=250)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-files", type=int, default=0)
    p.add_argument("--decision-cutoffs", default="0,1,3,5,10")
    p.add_argument("--outcome-horizons", default="1,3,5,10,30,60,180,300")
    args = p.parse_args(argv)

    symbols = tuple(s.strip().upper() for s in args.symbols.split(",") if s.strip())
    if set(symbols) - {"BTCUSDT", "DOGEUSDT"}:
        raise SystemExit("only BTCUSDT,DOGEUSDT allowed in V1")
    start = _parse_dt(args.start)
    end = _parse_dt(args.end)
    max_files = args.max_files or None

    # smoke flag only documents intent; window comes from --start/--end
    _ = args.smoke, args.decision_cutoffs, args.outcome_horizons, args.audit, args.analyze

    manifest = run_wall_attack_discovery(
        raw_root=args.raw_root,
        output_dir=args.output_dir,
        start=start,
        end=end,
        symbols=symbols,
        sample_ms=args.sample_ms,
        seed=args.seed,
        max_files=max_files,
    )
    print(json.dumps({k: manifest[k] for k in (
        "window_start_utc", "window_end_utc", "n_primary_attacks", "n_lifecycles",
        "resolution_60s_counts", "n_controls",
    )}, indent=2))
    print(f"wrote {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
