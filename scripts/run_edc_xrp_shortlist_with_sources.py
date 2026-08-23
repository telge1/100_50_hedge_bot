#!/usr/bin/env python3
"""Research-only: XRP shortlist with three-level source evaluation."""

from __future__ import annotations

import argparse
from datetime import datetime


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--timeframes", default="15m,5m")
    p.add_argument("--start", default="2026-07-23T00:00:00Z")
    p.add_argument("--end", default="2026-08-22T00:00:00Z")
    p.add_argument("--export-dir", default=None)
    args = p.parse_args()

    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.shortlist_runner import (
        run_xrp_shortlist_with_sources,
    )

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    tfs = tuple(x.strip() for x in args.timeframes.split(",") if x.strip())
    result = run_xrp_shortlist_with_sources(
        symbol=args.symbol,
        timeframes=tfs,
        window_start=start,
        window_end=end,
        export_dir=args.export_dir,
    )
    print("export_dir:", result["export_dir"])
    print("verdict:", result.get("verdict"))
    print("n_candidates:", result["n_candidates"])
    return 0 if result.get("verdict") == "XRP_SHORTLIST_SOURCE_FILTER_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
