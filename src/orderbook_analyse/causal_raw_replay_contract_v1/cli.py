"""CLI for CAUSAL_RAW_REPLAY_CONTRACT_V1 validation."""

from __future__ import annotations

import argparse
from pathlib import Path

from . import DEFAULT_OUT
from .runner import run_validation


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="CAUSAL_RAW_REPLAY_CONTRACT_V1 validation")
    p.add_argument("--out", type=Path, default=Path(DEFAULT_OUT))
    args = p.parse_args(argv)
    summary = run_validation(args.out)
    print(summary["verdict"])
    return 0 if summary["verdict"].startswith("RAW_REPLAY_CAUSAL_READY") else 1


if __name__ == "__main__":
    raise SystemExit(main())
