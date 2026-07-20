"""CLI for Emergency-Lock Phase A / B / C (offline research only)."""

from __future__ import annotations

import argparse
import json
import sys

from .config import EmergencyLockRecoveryConfig, apply_cli_overrides
from .phase_a_runner import EmergencyLockError, run_phase_a_to_disk
from .phase_b_runner import DEFAULT_PHASE_B_OUTPUT_DIR, run_phase_b_to_disk
from .phase_c_report import run_phase_c_to_disk
from .phase_c_runner import DEFAULT_PHASE_C_OUTPUT_DIR, phase_b_baseline_config
from .phase_d_report import run_phase_d_to_disk
from .phase_d_runner import DEFAULT_PHASE_D_OUTPUT_DIR
from .phase_d1_report import run_phase_d1_to_disk
from .phase_d1_runner import DEFAULT_PHASE_D1_OUTPUT_DIR
from .phase_f0_report import run_phase_f0_to_disk
from .phase_f0_runner import DEFAULT_PHASE_F0_OUTPUT_DIR


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Emergency-Lock Phase A/B/C/D/D.1/F0 offline backtester"
    )
    p.add_argument(
        "--phase",
        choices=("a", "b", "c", "d", "d1", "f0"),
        default="a",
        help="Phase to run (default: a)",
    )
    p.add_argument("--symbol", default=None)
    p.add_argument("--timeframe", default=None)
    p.add_argument("--start-timestamp", default=None, dest="start_timestamp")
    p.add_argument("--start-index", type=int, default=None, dest="start_index")
    p.add_argument("--max-candles", type=int, default=None, dest="max_candles")
    p.add_argument(
        "--emergency-trigger-pct",
        type=float,
        default=None,
        dest="emergency_trigger_pct",
    )
    p.add_argument("--fee-rate", type=float, default=None, dest="fee_rate")
    p.add_argument("--slippage-bps", type=float, default=None, dest="slippage_bps")
    p.add_argument(
        "--initial-long-notional-usdt",
        type=float,
        default=None,
        dest="initial_long_notional_usdt",
    )
    p.add_argument(
        "--initial-short-notional-usdt",
        type=float,
        default=None,
        dest="initial_short_notional_usdt",
    )
    p.add_argument(
        "--output-dir",
        default=None,
        dest="output_dir",
        help="Phase defaults: .../phase_a|phase_b|phase_c|phase_d|phase_d1|phase_f0",
    )
    p.add_argument(
        "--funding-enabled",
        action="store_true",
        default=None,
        dest="funding_enabled",
    )
    p.add_argument(
        "--basket-exit-buffer-usdt",
        type=float,
        default=None,
        dest="basket_exit_buffer_usdt",
    )
    p.add_argument(
        "--max-post-lock-bars",
        type=int,
        default=None,
        dest="max_post_lock_bars",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides = {
        "symbol": args.symbol,
        "timeframe": args.timeframe,
        "start_timestamp": args.start_timestamp,
        "start_index": args.start_index,
        "max_candles": args.max_candles,
        "emergency_trigger_pct": args.emergency_trigger_pct,
        "fee_rate": args.fee_rate,
        "slippage_bps": args.slippage_bps,
        "initial_long_notional_usdt": args.initial_long_notional_usdt,
        "initial_short_notional_usdt": args.initial_short_notional_usdt,
        "output_dir": args.output_dir,
        "funding_enabled": True if args.funding_enabled else None,
        "basket_exit_buffer_usdt": args.basket_exit_buffer_usdt,
        "max_post_lock_bars": args.max_post_lock_bars,
    }

    try:
        if args.phase == "a":
            cfg = apply_cli_overrides(EmergencyLockRecoveryConfig(), **overrides)
            result = run_phase_a_to_disk(cfg)
            summary = result["summary"]
            print(
                json.dumps(
                    {
                        "phase": "a",
                        "lock_triggered": summary["lock_triggered"],
                        "full_lock_invariant_passed": summary[
                            "full_lock_invariant_passed"
                        ],
                        "output_paths": result.get("output_paths"),
                    },
                    indent=2,
                )
            )
            return 0 if summary["full_lock_invariant_passed"] else 1

        if args.phase == "b":
            cfg = apply_cli_overrides(EmergencyLockRecoveryConfig(), **overrides)
            if cfg.output_dir == "research/backtests/results/emergency_lock/phase_a":
                cfg = apply_cli_overrides(cfg, output_dir=DEFAULT_PHASE_B_OUTPUT_DIR)
            result = run_phase_b_to_disk(cfg)
            summary = result["summary"]
            print(
                json.dumps(
                    {
                        "phase": "b",
                        "lock_triggered": summary["lock_triggered"],
                        "break_even_reached": summary["break_even_reached"],
                        "final_status": summary["final_status"],
                        "final_net_pnl": summary["final_net_pnl"],
                        "output_paths": result.get("output_paths"),
                    },
                    indent=2,
                )
            )
            return 0

        # Phase C
        if args.phase == "c":
            cfg = phase_b_baseline_config(
                symbol=args.symbol or "APTUSDT",
                timeframe=args.timeframe or "5m",
            )
            cfg = apply_cli_overrides(
                cfg, **{k: v for k, v in overrides.items() if k != "output_dir"}
            )
            out_dir = args.output_dir or DEFAULT_PHASE_C_OUTPUT_DIR
            result = run_phase_c_to_disk(cfg=cfg, output_dir=out_dir)
            from pathlib import Path

            summary = json.loads(
                Path(result["output_paths"]["aggregate_summary_json"]).read_text(
                    encoding="utf-8"
                )
            )
            print(
                json.dumps(
                    {
                        "phase": "c",
                        "raw_candidate_count": summary["raw_candidate_count"],
                        "deduped_event_count": summary["deduped_event_count"],
                        "output_paths": result.get("output_paths"),
                    },
                    indent=2,
                )
            )
            return 0

        # Phase D
        if args.phase == "d":
            out_dir = args.output_dir or DEFAULT_PHASE_D_OUTPUT_DIR
            result = run_phase_d_to_disk(output_dir=out_dir)
            from pathlib import Path

            summary = json.loads(
                Path(result["output_paths"]["phase_d_summary_json"]).read_text(
                    encoding="utf-8"
                )
            )
            print(
                json.dumps(
                    {
                        "phase": "d",
                        "event_count": summary["event_count"],
                        "phase_e_candidates": summary.get("phase_e_candidates"),
                        "protected_structure_adapter_available": summary.get(
                            "protected_structure_adapter_available"
                        ),
                        "output_paths": result.get("output_paths"),
                    },
                    indent=2,
                )
            )
            return 0

        # Phase D.1
        if args.phase == "d1":
            out_dir = args.output_dir or DEFAULT_PHASE_D1_OUTPUT_DIR
            result = run_phase_d1_to_disk(output_dir=out_dir)
            from pathlib import Path

            manifest = json.loads(
                Path(result["output_paths"]["manifest_json"]).read_text(encoding="utf-8")
            )
            print(
                json.dumps(
                    {
                        "phase": "d1",
                        "event_count": manifest["event_count"],
                        "phase_e_candidates": manifest.get("phase_e_candidates"),
                        "output_paths": result.get("output_paths"),
                    },
                    indent=2,
                )
            )
            return 0

        # Phase F0
        out_dir = args.output_dir or DEFAULT_PHASE_F0_OUTPUT_DIR
        result = run_phase_f0_to_disk(output_dir=out_dir)
        from pathlib import Path

        manifest = json.loads(
            Path(result["output_paths"]["manifest_json"]).read_text(encoding="utf-8")
        )
        print(
            json.dumps(
                {
                    "phase": "f0",
                    "event_count": manifest["event_count"],
                    "leg_count": manifest.get("leg_count"),
                    "phase_f1_candidates": manifest.get("phase_f1_candidates"),
                    "output_paths": result.get("output_paths"),
                },
                indent=2,
            )
        )
        return 0
    except EmergencyLockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
