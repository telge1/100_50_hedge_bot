"""CLI entrypoint for Cobertura-0-Notional recovery backtests."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import CoberturaConfig, default_apt_example
from .runner import run_cobertura


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Cobertura-0-Notional recovery backtester")
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to JSON config (default: built-in APT example)",
    )
    p.add_argument("--output-dir", type=str, default=None)
    p.add_argument("--run-id", type=str, default=None)
    p.add_argument("--add-size-pct", type=float, default=None)
    p.add_argument("--activation-move-pct", type=float, default=None)
    p.add_argument("--first-add-move-pct", type=float, default=None)
    p.add_argument("--add-step-pct", type=float, default=None)
    p.add_argument("--max-add-count", type=int, default=None)
    p.add_argument("--slippage-bps", type=float, default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.config:
        cfg = CoberturaConfig.from_json(args.config)
    else:
        cfg = default_apt_example()

    if args.output_dir:
        cfg.output_dir = args.output_dir
    if args.run_id:
        cfg.run_id = args.run_id
    if args.add_size_pct is not None:
        cfg.add_size_pct = float(args.add_size_pct)
    if args.activation_move_pct is not None:
        cfg.activation_move_pct = float(args.activation_move_pct)
    if args.first_add_move_pct is not None:
        cfg.first_add_move_pct = float(args.first_add_move_pct)
    if args.add_step_pct is not None:
        cfg.add_step_pct = float(args.add_step_pct)
    if args.max_add_count is not None:
        cfg.max_add_count = int(args.max_add_count)
    if args.slippage_bps is not None:
        cfg.slippage_bps_open = float(args.slippage_bps)
        cfg.slippage_bps_close = float(args.slippage_bps)

    cfg.validate()
    result = run_cobertura(cfg, write_outputs=True)
    print(
        json.dumps(
            {
                "state": result.state,
                "exit_reason": result.exit_reason,
                "recovery_rounds": result.recovery_rounds,
                "bars_processed": result.bars_processed,
                "locked_spread_loss": result.locked_spread_loss,
                "output_dir": cfg.output_dir or cfg.run_id,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
