"""CLI for multi-source data inventory audit V1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import OA_ROOT, run_audit


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only multi-source data inventory audit V1")
    p.add_argument(
        "--out",
        type=Path,
        default=OA_ROOT / "results" / "multisource_data_inventory_audit_v1",
        help="Output directory",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_audit(args.out)
    print(f"verdict={result['summary']['verdict']}")
    print(f"out={result['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
