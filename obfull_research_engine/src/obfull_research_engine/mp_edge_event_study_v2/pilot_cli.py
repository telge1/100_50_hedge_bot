"""CLI for MP edge event pilot V2."""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from .params import (
    DEFAULT_ABSORB_CONFIRMATION_BPS,
    DEFAULT_ABSORB_CONFIRMATION_MAX_S,
    DEFAULT_APPROACH_LOOKBACK_S,
    DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS,
    DEFAULT_CONFLUENCE_TOLERANCE_BPS,
    DEFAULT_END,
    DEFAULT_FAILED_BREAK_HORIZON_S,
    DEFAULT_MIN_EVENT_SEPARATION_S,
    DEFAULT_MIN_PENETRATION_BPS,
    DEFAULT_OUTCOME_HORIZONS_S,
    DEFAULT_RECLAIM_HOLD_S,
    DEFAULT_RESET_DISTANCE_BPS,
    DEFAULT_START,
    DEFAULT_SYMBOL,
    DEFAULT_TOUCH_TOLERANCE_BPS,
    DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
    DEFAULT_TRUE_BREAK_CONTINUATION_BPS,
    SILVER_DATABASE_DEFAULT,
    build_params_v2,
)
from .pipeline import run_pilot_v2
from .report import choose_verdict_v2, load_v1_summary, write_pilot_report_v2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mp_edge_event_study_v2.pilot_cli")
    p.add_argument("--symbol", default=DEFAULT_SYMBOL)
    p.add_argument("--start", default=DEFAULT_START)
    p.add_argument("--end", default=DEFAULT_END)
    p.add_argument("--touch-tolerance-bps", type=float, default=DEFAULT_TOUCH_TOLERANCE_BPS)
    p.add_argument("--confluence-tolerance-bps", type=float, default=DEFAULT_CONFLUENCE_TOLERANCE_BPS)
    p.add_argument("--min-event-separation-s", type=float, default=DEFAULT_MIN_EVENT_SEPARATION_S)
    p.add_argument("--reset-distance-bps", type=float, default=DEFAULT_RESET_DISTANCE_BPS)
    p.add_argument("--min-penetration-bps", type=float, default=DEFAULT_MIN_PENETRATION_BPS)
    p.add_argument("--reclaim-hold-s", type=float, default=DEFAULT_RECLAIM_HOLD_S)
    p.add_argument("--failed-break-horizon-s", type=float, default=DEFAULT_FAILED_BREAK_HORIZON_S)
    p.add_argument("--true-break-acceptance-s", type=float, default=DEFAULT_TRUE_BREAK_ACCEPTANCE_S)
    p.add_argument(
        "--true-break-continuation-bps", type=float, default=DEFAULT_TRUE_BREAK_CONTINUATION_BPS
    )
    p.add_argument("--approach-lookback-s", type=float, default=DEFAULT_APPROACH_LOOKBACK_S)
    p.add_argument(
        "--approach-origin-distance-bps", type=float, default=DEFAULT_APPROACH_ORIGIN_DISTANCE_BPS
    )
    p.add_argument("--require-correct-approach", action="store_true", default=True)
    p.add_argument("--no-require-correct-approach", action="store_false", dest="require_correct_approach")
    p.add_argument("--allow-retest-from-break-side", action="store_true", default=False)
    p.add_argument("--absorb-confirmation-bps", type=float, default=DEFAULT_ABSORB_CONFIRMATION_BPS)
    p.add_argument("--absorb-confirmation-max-s", type=float, default=DEFAULT_ABSORB_CONFIRMATION_MAX_S)
    p.add_argument(
        "--outcome-horizons-s",
        default=",".join(str(x) for x in DEFAULT_OUTCOME_HORIZONS_S),
    )
    p.add_argument("--silver-database", default=SILVER_DATABASE_DEFAULT)
    p.add_argument("--output-dir", required=True)
    p.add_argument(
        "--v1-run-dir",
        default="obfull_research_engine/runs/mp_edge_event_pilot_v1_20260916",
    )
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = build_params_v2(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        touch_tolerance_bps=args.touch_tolerance_bps,
        confluence_tolerance_bps=args.confluence_tolerance_bps,
        min_event_separation_s=args.min_event_separation_s,
        reset_distance_bps=args.reset_distance_bps,
        min_penetration_bps=args.min_penetration_bps,
        reclaim_hold_s=args.reclaim_hold_s,
        failed_break_horizon_s=args.failed_break_horizon_s,
        true_break_acceptance_s=args.true_break_acceptance_s,
        true_break_continuation_bps=args.true_break_continuation_bps,
        approach_lookback_s=args.approach_lookback_s,
        approach_origin_distance_bps=args.approach_origin_distance_bps,
        require_correct_approach=bool(args.require_correct_approach),
        allow_retest_from_break_side=bool(args.allow_retest_from_break_side),
        absorb_confirmation_bps=args.absorb_confirmation_bps,
        absorb_confirmation_max_s=args.absorb_confirmation_max_s,
        outcome_horizons_s=args.outcome_horizons_s,
        output_dir=args.output_dir,
        dry_run=bool(args.dry_run),
        silver_database=args.silver_database,
        v1_run_dir=args.v1_run_dir,
    )
    t0 = time.time()
    try:
        result = run_pilot_v2(params)
    except Exception as exc:  # noqa: BLE001
        blocked = str(exc)
        params.output_dir.mkdir(parents=True, exist_ok=True)
        (params.output_dir / "pilot_v2.log").write_text(
            f"FAIL {time.time()-t0:.1f}s\n{blocked}\n{traceback.format_exc()}\n",
            encoding="utf-8",
        )
        verdict = choose_verdict_v2(n_events=0, blocked=blocked)
        print(f"VERDICT={verdict}")
        print(f"ERROR={blocked}")
        return 2

    if params.dry_run:
        info = result["dry_run"]
        print("DRY_RUN_OK")
        for k in (
            "database",
            "window_start",
            "window_end",
            "replay_epoch",
            "estimated_mid_rows",
            "output_dir",
            "db_mutation",
        ):
            print(f"{k}={info.get(k)}")
        return 0

    verdict = choose_verdict_v2(n_events=len(result["events"]))
    if time.time() - t0 > 1800:
        verdict = "MP_PILOT_V2_RESOURCE_ABORT"
    v1_sum = load_v1_summary(Path(params.v1_run_dir))
    write_pilot_report_v2(
        Path(params.output_dir) / "PILOT_REPORT_V2.md",
        params=params,
        result=result,
        verdict=verdict,
        v1_summary=v1_sum,
    )
    print(f"VERDICT={verdict}")
    print(f"OUTPUT={params.output_dir}")
    print(f"EVENTS={len(result['events'])}")
    print(f"ELAPSED_S={time.time()-t0:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
