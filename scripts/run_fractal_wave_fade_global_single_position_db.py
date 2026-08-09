#!/usr/bin/env python3
"""Global single-position wave-fade backtest (MySQL SoT; frozen strategy)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_global_single_position_db.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_db.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_wave_fade_global_single_position_db",
    )
    args = p.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    print(f"[primary] {payload['decision']}", flush=True)
    print(
        f"[window] {payload['common_start']} → {payload['common_end']}",
        flush=True,
    )
    o, n = payload["old_additive"], payload["new_additive"]
    print(
        f"[trades] old={o['trades']} new={n['trades']} | "
        f"exp old={o['expectancy']} new={n['expectancy']} | "
        f"PF old={o['profit_factor']} new={n['profit_factor']}",
        flush=True,
    )
    for tag in ("25", "50", "100"):
        s = payload["fraction_summaries"][tag]
        print(f"[equity {tag}%] end={s['end_equity']:.2f}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
