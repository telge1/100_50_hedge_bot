#!/usr/bin/env python3
"""Dynamic P5 cluster exit-upgrade research (MySQL SoT, DOGE/BTC)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.export import write_results  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_dynamic_cluster_upgrade_db",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    dec = payload.get("decisions") or {}
    print(f"[primary] {dec.get('primary')}", flush=True)
    print(f"[sl_policy] {dec.get('sl_policy')}", flush=True)
    print(f"[giveback] {dec.get('giveback')}", flush=True)
    print(f"[conflict] {dec.get('conflict')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
