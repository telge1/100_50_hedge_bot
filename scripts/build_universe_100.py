#!/usr/bin/env python3
"""Build versioned ~100-coin Bybit linear USDT perpetual universe."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.universe import (  # noqa: E402
    MUST_INCLUDE,
    build_universe,
    save_universe,
)

DEFAULT_OUT = ROOT / "config" / "universe_100.json"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--target-size", type=int, default=100)
    args = p.parse_args(argv)

    universe = build_universe(target_size=args.target_size, must_include=MUST_INCLUDE)
    save_universe(universe, args.out)
    print(f"Wrote {len(universe.symbols)} symbols → {args.out}")
    print("Top 10:", ", ".join(universe.symbols[:10]))
    missing = [s for s in MUST_INCLUDE if s not in universe.symbols]
    if missing:
        print("ERROR missing must-include:", missing, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
