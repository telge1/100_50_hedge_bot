#!/usr/bin/env python3
"""Run directional control analysis from existing fractal wave CSVs (APTUSDT)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_directional_control.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_directional_control.export import write_results  # noqa: E402
from orderbook_analyse.fractal_directional_control.load_join import (  # noqa: E402
    DEFAULT_WAVE_DIR,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--wave-dir", type=Path, default=DEFAULT_WAVE_DIR)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_directional_control_apt",
    )
    args = p.parse_args(argv)

    payload = run_analysis(wave_dir=args.wave_dir)
    paths = write_results(payload, args.out_dir)
    dec = payload.get("decisions") or {}
    print(f"[decision] {dec.get('directional_control')}", flush=True)
    print(f"[decision] {dec.get('cci_turn')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
