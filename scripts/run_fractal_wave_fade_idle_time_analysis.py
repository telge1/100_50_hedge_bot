#!/usr/bin/env python3
"""Idle / wait-time analysis for validated global-single trades."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_idle_time_analysis.analysis import (  # noqa: E402
    run_analysis,
)


def main() -> int:
    p = run_analysis()
    s = p["stats"]
    c = p["cumulative"]
    b = p["budget"]
    cap = p["capacity"]

    print()
    print("IDLE_TIME_ANALYSIS_READY")
    print(f"- Trades: {p['n_trades']}")
    print(f"- Mean idle hours: {s['hours']['mean']:.2f}")
    print(f"- Median idle hours: {s['hours']['median']:.2f}")
    print(f"- P90 idle hours: {s['hours']['p90']:.2f}")
    print(f"- Max idle hours: {s['hours']['max']:.2f}")
    print(f"- % next trade <1h: {c['within_1h']['share_pct']:.1f}%")
    print(f"- % <3h: {c['within_3h']['share_pct']:.1f}%")
    print(f"- % <6h: {c['within_6h']['share_pct']:.1f}%")
    print(f"- % <12h: {c['within_12h']['share_pct']:.1f}%")
    print(f"- % <24h: {c['within_24h']['share_pct']:.1f}%")
    print(f"- Time in market %: {b['time_in_market_pct']:.1f}%")
    print(f"- Flat idle %: {b['flat_idle_pct']:.1f}%")
    print()
    print(f"Capacity: {cap['unused_time_level']}")
    print(cap["descriptive_note"])
    print(f"out: {p['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
