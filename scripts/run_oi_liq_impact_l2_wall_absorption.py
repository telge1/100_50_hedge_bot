#!/usr/bin/env python3
"""Run BTC F3 wall-absorption discovery with mandatory data-availability audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.oi_liq_impact_l2.wall_absorption.discovery import (  # noqa: E402
    DEFAULT_F1_DIR,
    DEFAULT_F2_DIR,
    DEFAULT_OUTPUT_DIR,
    WallAbsorptionError,
    run_wall_absorption_discovery,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "BTC F3 wall-absorption discovery; requires causal per-level "
            "orderbook reconstruction."
        )
    )
    parser.add_argument("--f1-dir", type=Path, default=DEFAULT_F1_DIR)
    parser.add_argument("--f2-dir", type=Path, default=DEFAULT_F2_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--files-root", type=Path, default=None)
    parser.add_argument(
        "--skip-clickhouse",
        action="store_true",
        help="Audit filesystem only; do not query ClickHouse.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_wall_absorption_discovery(
            f1_dir=args.f1_dir,
            f2_dir=args.f2_dir,
            output_dir=args.output_dir,
            files_root=args.files_root,
            query_clickhouse=not args.skip_clickhouse,
        )
    except WallAbsorptionError as exc:
        print("BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print("BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - controlled CLI boundary
        print("BTC_F3_WALL_ABSORPTION_DISCOVERY_BLOCKED", file=sys.stderr)
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    print(result.verdict)
    print(f"output_dir={result.output_dir}")
    return 0 if result.passed_audit else 2


if __name__ == "__main__":
    raise SystemExit(main())
