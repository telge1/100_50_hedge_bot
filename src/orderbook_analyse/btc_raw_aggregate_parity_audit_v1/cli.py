"""CLI for BTC raw vs aggregate parity root-cause audit."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import DEFAULT_OUT
from .runner import run_audit


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="BTC raw vs aggregate parity root-cause audit V1")
    p.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    args = p.parse_args(argv)
    run_audit(args.out)
    return 0
