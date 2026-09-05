#!/usr/bin/env python3
"""Read-only audit of public trade impact compression in BTC F3 artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.constants import (  # noqa: E402
    DEFAULT_HORIZONS,
    DEFAULT_INPUT_DIR,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_WINDOWS,
)
from orderbook_analyse.oi_liq_impact_l2.public_trade_audit.runner import (  # noqa: E402
    run_public_trade_audit,
)


def _parse_int_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(part.strip()) for part in text.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit public trade impact compression from BTC F3 artifacts."
    )
    parser.add_argument("--input-dir", type=Path, default=Path(DEFAULT_INPUT_DIR))
    parser.add_argument("--output-dir", type=Path, default=Path(DEFAULT_OUTPUT_DIR))
    parser.add_argument(
        "--windows",
        default=",".join(str(v) for v in DEFAULT_WINDOWS),
        help="Comma-separated fixed window sizes (5,10).",
    )
    parser.add_argument(
        "--horizons",
        default=",".join(str(v) for v in DEFAULT_HORIZONS),
        help="Comma-separated post-compression outcome horizons in minutes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    windows = _parse_int_tuple(args.windows)
    horizons = _parse_int_tuple(args.horizons)
    result = run_public_trade_audit(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        windows=windows,
        horizons=horizons,
    )
    print(result.verdict)
    if result.blocked and result.missing_fields:
        for field in result.missing_fields:
            print(f"missing_field:{field}")
    return 0 if result.verdict.endswith("COMPLETE") else 2


if __name__ == "__main__":
    raise SystemExit(main())
