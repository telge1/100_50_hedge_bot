"""CLI for BTC/DOGE current multi-source recheck."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import DEFAULT_OUT
from .runner import run_recheck


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="BTC/DOGE current multi-source recheck V1 (read-only)")
    p.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    args = p.parse_args(argv)
    run_recheck(args.out)
    return 0
