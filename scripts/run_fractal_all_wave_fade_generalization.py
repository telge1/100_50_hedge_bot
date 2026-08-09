#!/usr/bin/env python3
"""Frozen all-wave Stoch fade OOS / cross-symbol generalization (APT/DOGE/BTC)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_all_wave_fade_generalization.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_all_wave_fade_generalization.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_all_wave_fade_generalization",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis(args.out_dir)
    paths = write_results(payload, args.out_dir)
    print(f"[primary] {payload.get('primary_decision')}", flush=True)
    print(f"[failure] {payload.get('failure_filter_decision')}", flush=True)
    print(f"[pivot] {payload.get('pivot_utility_decision')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
