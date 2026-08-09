#!/usr/bin/env python3
"""APTUSDT: detect later-confirmed 15m wave failures during the live wave."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_15m_failure_early_detection.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_15m_failure_early_detection.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_15m_failure_early_detection_apt",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    dec = payload.get("decisions") or {}
    print(f"[decision] {dec.get('primary')}", flush=True)
    print(f"[decision] {dec.get('partial_efficiency')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
