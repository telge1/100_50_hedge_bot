#!/usr/bin/env python3
"""Research-only CLI: EDC sync tolerance Phase-1 (M0 + M1 gaps 0/1/2)."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description="EDC sync tolerance Phase-1 pilot (research-only)")
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--timeframes", default="15m,5m")
    p.add_argument("--start", default="2026-07-23T00:00:00Z")
    p.add_argument("--end", default="2026-08-22T00:00:00Z")
    p.add_argument("--export-root", default=None, help="defaults to results/edc_sync_tolerance/<run_id>")
    p.add_argument("--run-id", default=None)
    args = p.parse_args()

    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.runner import run_sync_tolerance_pilot

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    tfs = tuple(x.strip() for x in args.timeframes.split(",") if x.strip())
    result = run_sync_tolerance_pilot(
        symbol=args.symbol,
        timeframes=tfs,
        window_start=start,
        window_end=end,
        export_root=args.export_root,
        run_id=args.run_id,
    )
    print("export_dir:", result["export_dir"])
    print("parity_all_ok:", result["parity_all_ok"])
    print("parity:", result["parity"])
    print("n_candidates:", result["n_candidates"])
    print("n_trade_rows:", result["n_trade_rows"])
    return 0 if result["parity_all_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
