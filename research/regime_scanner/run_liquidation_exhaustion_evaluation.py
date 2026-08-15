"""CLI: offline evaluation of liquidation exhaustion full-run artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from research.regime_scanner.liquidation_exhaustion.evaluate import run_evaluation


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_liquidation_exhaustion_evaluation")
    p.add_argument("--input-dir", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p.add_argument("--mode", choices=("full",), default="full")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.input_dir.is_dir():
        print(f"ERROR: input-dir not found: {args.input_dir}", file=sys.stderr)
        return 2
    required = [
        "deduplicated_events.csv",
        "forward_outcomes.csv",
        "event_clusters.csv",
        "reclaim_events.csv",
        "controls.csv",
    ]
    missing = [f for f in required if not (args.input_dir / f).exists()]
    if missing:
        print(f"ERROR: missing input files: {missing}", file=sys.stderr)
        return 2
    try:
        integrity = run_evaluation(
            input_dir=args.input_dir, output_dir=args.output_dir, mode=args.mode
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
    print(
        f"status=ok mode={args.mode} decision={integrity.get('decision')} "
        f"physical_anchors={integrity.get('physical_anchors')} "
        f"gate_all_pass={integrity.get('gate_all_pass')} "
        f"gate_hard_pass={integrity.get('gate_hard_pass')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
