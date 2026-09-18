"""CLI for the BTCUSDT MP-edge event pilot."""

from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

from .params import (
    DEFAULT_CONFLUENCE_TOLERANCE_BPS,
    DEFAULT_END,
    DEFAULT_MAX_RECLAIM_DELAY_S,
    DEFAULT_MIN_EVENT_SEPARATION_S,
    DEFAULT_MIN_PENETRATION_BPS,
    DEFAULT_OUTCOME_HORIZONS_S,
    DEFAULT_RECLAIM_HOLD_S,
    DEFAULT_RECLAIM_TOLERANCE_BPS,
    DEFAULT_RESET_DISTANCE_BPS,
    DEFAULT_SL_TARGET_BPS,
    DEFAULT_START,
    DEFAULT_SYMBOL,
    DEFAULT_TOUCH_TOLERANCE_BPS,
    DEFAULT_TP_TARGETS_BPS,
    DEFAULT_TRUE_BREAK_ACCEPTANCE_S,
    SILVER_DATABASE_DEFAULT,
    build_params,
)
from .pipeline import run_pilot, summarize
from .report import choose_verdict, pick_sample_events, write_pilot_report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mp_edge_event_study_v1.pilot_cli",
        description="Causal previous_closed TPO VAH/VAL MP-edge event pilot",
    )
    p.add_argument("--symbol", default=DEFAULT_SYMBOL)
    p.add_argument("--start", default=DEFAULT_START, help="UTC Z timestamp")
    p.add_argument("--end", default=DEFAULT_END, help="UTC Z timestamp")
    p.add_argument("--touch-tolerance-bps", type=float, default=DEFAULT_TOUCH_TOLERANCE_BPS)
    p.add_argument(
        "--confluence-tolerance-bps", type=float, default=DEFAULT_CONFLUENCE_TOLERANCE_BPS
    )
    p.add_argument(
        "--min-event-separation-s", type=float, default=DEFAULT_MIN_EVENT_SEPARATION_S
    )
    p.add_argument("--reset-distance-bps", type=float, default=DEFAULT_RESET_DISTANCE_BPS)
    p.add_argument("--min-penetration-bps", type=float, default=DEFAULT_MIN_PENETRATION_BPS)
    p.add_argument("--max-reclaim-delay-s", type=float, default=DEFAULT_MAX_RECLAIM_DELAY_S)
    p.add_argument("--reclaim-hold-s", type=float, default=DEFAULT_RECLAIM_HOLD_S)
    p.add_argument(
        "--true-break-acceptance-s", type=float, default=DEFAULT_TRUE_BREAK_ACCEPTANCE_S
    )
    p.add_argument(
        "--outcome-horizons-s",
        default=",".join(str(x) for x in DEFAULT_OUTCOME_HORIZONS_S),
    )
    p.add_argument(
        "--tp-targets-bps",
        default=",".join(str(int(x)) for x in DEFAULT_TP_TARGETS_BPS),
    )
    p.add_argument("--sl-target-bps", type=float, default=DEFAULT_SL_TARGET_BPS)
    p.add_argument("--reclaim-tolerance-bps", type=float, default=DEFAULT_RECLAIM_TOLERANCE_BPS)
    p.add_argument("--silver-database", default=SILVER_DATABASE_DEFAULT)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    params = build_params(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        touch_tolerance_bps=args.touch_tolerance_bps,
        confluence_tolerance_bps=args.confluence_tolerance_bps,
        min_event_separation_s=args.min_event_separation_s,
        reset_distance_bps=args.reset_distance_bps,
        min_penetration_bps=args.min_penetration_bps,
        max_reclaim_delay_s=args.max_reclaim_delay_s,
        reclaim_hold_s=args.reclaim_hold_s,
        true_break_acceptance_s=args.true_break_acceptance_s,
        outcome_horizons_s=args.outcome_horizons_s,
        tp_targets_bps=args.tp_targets_bps,
        sl_target_bps=args.sl_target_bps,
        output_dir=args.output_dir,
        dry_run=bool(args.dry_run),
        silver_database=args.silver_database,
        reclaim_tolerance_bps=args.reclaim_tolerance_bps,
    )
    t0 = time.time()
    try:
        result = run_pilot(params)
    except Exception as exc:  # noqa: BLE001
        blocked = str(exc)
        out = Path(params.output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "pilot.log").write_text(
            f"FAIL after {time.time()-t0:.1f}s\n{blocked}\n{traceback.format_exc()}\n",
            encoding="utf-8",
        )
        verdict = choose_verdict(n_events=0, blocked=blocked)
        if (
            "RESOURCE" in blocked.upper()
            or "MEMORY" in blocked.upper()
            or (time.time() - t0) > 1800
        ):
            verdict = "MP_PILOT_RESOURCE_ABORT"
        print(f"VERDICT={verdict}")
        print(f"ERROR={blocked}")
        return 2

    if params.dry_run:
        info = result["dry_run"]
        print("DRY_RUN_OK")
        for k in (
            "database",
            "metrics_table",
            "window_start",
            "window_end",
            "replay_epoch",
            "estimated_mid_rows",
            "planned_mp_period_ends",
            "output_dir",
            "db_mutation",
        ):
            print(f"{k}={info.get(k)}")
        return 0

    result["summary"] = summarize(result, params)
    samples = pick_sample_events(result["events"])
    verdict = choose_verdict(n_events=len(result["events"]))
    elapsed = time.time() - t0
    if elapsed > 1800:
        verdict = "MP_PILOT_RESOURCE_ABORT"
    write_pilot_report(
        Path(params.output_dir) / "PILOT_REPORT.md",
        params=params,
        result=result,
        verdict=verdict,
        sample_events=samples,
    )
    print(f"VERDICT={verdict}")
    print(f"OUTPUT={params.output_dir}")
    print(f"EVENTS={len(result['events'])}")
    print(f"ELAPSED_S={elapsed:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
