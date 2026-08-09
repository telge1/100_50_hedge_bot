#!/usr/bin/env python3
"""Cashout/reserve analysis on validated global-single-position trades."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_wave_fade_cashout_reserve_analysis",
    )
    args = p.parse_args(argv)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    c = payload["controls"]
    print(
        f"[done] trades={c['n_trades']} TP={c['tp']} SL={c['sl']} "
        f"maxSL={payload['sl_streak']['max_length']} "
        f"maxLose={payload['losing_streak']['max_length']}",
        flush=True,
    )
    for _, r in payload["comparison"].iterrows():
        print(
            f"  {int(r['cashout_rate_pct']):2d}%  active={r['end_active']:.2f} "
            f"reserve={r['end_reserve']:.2f} total={r['end_total_wealth']:.2f} "
            f"actDD={r['active_max_dd_pct']:.2f}% totDD={r['total_max_dd_pct']:.2f}% "
            f"cover={r['RESERVE_COVERS_MAX_DD']}",
            flush=True,
        )
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
