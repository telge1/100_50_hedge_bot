#!/usr/bin/env python3
"""Walk-forward + honest TRUE-OOS validation of frozen wave-fade strategy."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_walkforward_validation_db.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_walkforward_validation_db.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_wave_fade_walkforward_validation_db",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    if (payload.get("reopt_check") or {}).get("status") == "FAIL_VALIDATION":
        print("[FAIL_VALIDATION]", payload["reopt_check"].get("diffs"), flush=True)
        write_results(payload, args.out_dir)
        return 2
    paths = write_results(payload, args.out_dir)
    dec = payload.get("decisions") or {}
    print(f"[primary] {dec.get('primary')}", flush=True)
    print(f"[walk_forward] {dec.get('walk_forward')}", flush=True)
    print(f"[p5a] {dec.get('p5a')}", flush=True)
    print(f"[tier] {dec.get('tier')}", flush=True)
    print(f"[costs] {dec.get('costs')}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
