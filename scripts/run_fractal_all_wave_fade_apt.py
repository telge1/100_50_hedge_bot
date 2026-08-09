#!/usr/bin/env python3
"""APTUSDT: all completed Stoch-wave fade audit."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_all_wave_fade.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_all_wave_fade.export import write_results  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_all_wave_fade_apt",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    print(f"[overall] {payload.get('overall_decision')}", flush=True)
    print(f"[failure_filter] {payload.get('failure_filter_decision')}", flush=True)
    for tf, dec in (payload.get("tf_decisions") or {}).items():
        print(f"  {tf}: {dec}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
