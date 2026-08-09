#!/usr/bin/env python3
"""APTUSDT: fixed TP/SL matrix on T0 15m-failure confirmation entries."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_15m_failure_tpsl.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_15m_failure_tpsl.export import write_results  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_15m_failure_tpsl_apt",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    dec = payload.get("decisions") or {}
    print(f"[decision] {dec.get('primary')}", flush=True)
    print(f"[decision] {dec.get('long_short')}", flush=True)
    print(f"[decision] {dec.get('fixed_sl')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
