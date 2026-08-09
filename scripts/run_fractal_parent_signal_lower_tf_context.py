#!/usr/bin/env python3
"""1h/4h Tier-A parent signal × causal lower-TF Stoch context (DOGE/BTC)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_parent_signal_lower_tf_context.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_parent_signal_lower_tf_context.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_parent_signal_lower_tf_context",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    dec = payload.get("decisions") or {}
    print(f"[primary] {dec.get('primary')}", flush=True)
    print(f"[extended] {dec.get('extended')}", flush=True)
    print(f"[ready] {dec.get('ready')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
