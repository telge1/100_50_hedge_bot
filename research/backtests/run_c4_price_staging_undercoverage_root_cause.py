#!/usr/bin/env python3
"""CLI helper: reconstruct two_early_medium C4 undercoverage root cause.

Writes under:
  research/backtests/results/multicoin_price_staging_grid_1000_500_20260721/analysis/c4_undercoverage_root_cause/

Does not mutate grid raw artifacts. Does not start a full grid.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import run_isolated_blocker
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.recovery_reentry_policy import load_baseline_blockers
from research.backtests.second_leg_price_staging import resolve_grid_profile, resolve_profile

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GRID = (
    ROOT
    / "research/backtests/results/multicoin_price_staging_grid_1000_500_20260721"
)
DEFAULT_BASELINE = (
    ROOT
    / "research/backtests/results/current_baseline_multicoin_continuous_blocker_audit_20260720"
)
DEFAULT_OUT = DEFAULT_GRID / "analysis/c4_undercoverage_root_cause"


def _trace(coin: str, *, start: int, trade: int, profile: str, candles):
    cfg = resolve_profile(profile) if profile == "legacy" else resolve_grid_profile(profile)
    result = run_isolated_blocker(
        coin=coin,
        candles=candles,
        start_index=start,
        staging_config=cfg,
        trade_number=trade,
    )
    sr_fills = [
        f
        for f in (result.fill_log or [])
        if str(f.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
    ]
    cancels = [
        o
        for o in (result.order_log or [])
        if str(o.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
        and str(o.get("event_type") or "").lower() == "cancelled"
    ]
    audit = [
        a
        for a in build_pnl_coverage_audit(result)
        if int(a.get("cycle_index") or 0) == 4 and "LONG_ADD" in str(a.get("loss_purpose") or "")
    ]
    return {
        "profile": profile,
        "status": result.final_status,
        "exit_reason": result.exit_reason,
        "n_c4_sr_fills": len(sr_fills),
        "filled_stages": [
            (f.get("metadata_excerpt") or {}).get("stage_index") for f in sr_fills
        ],
        "n_cancels": len(cancels),
        "cancelled_stages": [
            (o.get("metadata_excerpt") or {}).get("stage_index") for o in cancels
        ],
        "c4_audit_status": audit[0].get("status") if audit else None,
        "c4_missing_pnl": audit[0].get("missing_pnl") if audit else None,
        "c4_cover_pnl": audit[0].get("cover_pnl") if audit else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="APTUSDT")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE)
    args = parser.parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    blockers = {
        str(b["coin"]).upper(): b
        for b in load_baseline_blockers(args.baseline_dir / "blocker_trades.csv")
    }
    b = blockers[args.coin.upper()]
    candles = normalize_candles(
        args.coin.upper(), load_candles_for_symbol(args.coin.upper(), limit=50000)
    )
    payload = {
        "coin": args.coin.upper(),
        "trade": int(b["trade_number"]),
        "start_index": int(b["start_index"]),
        "profiles": {
            name: _trace(
                args.coin.upper(),
                start=int(b["start_index"]),
                trade=int(b["trade_number"]),
                profile=name,
                candles=candles,
            )
            for name in ("legacy", "two_early_medium", "two_equal")
        },
    }
    out = args.output_dir / f"quick_trace_{args.coin.upper()}.json"
    out.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
