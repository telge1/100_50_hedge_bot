#!/usr/bin/env python3
"""Research-only: XRP 30m shortlist + horizon audit."""

from __future__ import annotations

import argparse
from datetime import datetime


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="XRPUSDT")
    p.add_argument("--start", default="2026-07-23T00:00:00Z")
    p.add_argument("--end", default="2026-08-22T00:00:00Z")
    p.add_argument("--export-dir", default=None)
    p.add_argument("--prior-dir", default=None)
    args = p.parse_args()

    from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.horizon_30m_runner import (
        run_xrp_30m_horizon_research,
    )

    start = datetime.fromisoformat(args.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00"))
    result = run_xrp_30m_horizon_research(
        symbol=args.symbol,
        window_start=start,
        window_end=end,
        export_dir=args.export_dir,
        prior_dir=args.prior_dir,
    )
    print("export_dir:", result["export_dir"])
    print("verdict:", result.get("verdict"))
    print("n_30m:", result["n_candidates_30m"])
    return 0 if result.get("verdict") == "XRP_30M_HORIZON_RESEARCH_READY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
