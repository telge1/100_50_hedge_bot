#!/usr/bin/env python3
"""BE50 exit replay on July 2026 fractal wave-fade trades."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_be50_july_2026.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_wave_fade_be50_july_2026.export import write_results  # noqa: E402


def main() -> int:
    payload = run_analysis()
    write_results(payload, payload["out_dir"])
    b, e = payload["base_summary"], payload["be_summary"]
    c = payload["counts"]
    print()
    print("BE50_JULY_REPLAY_READY")
    print(f"Primary Decision: {payload['decision']}")
    print(f"Baseline End Total: {b['end_total']:.2f} ({b['performance_pct']:+.2f}%)")
    print(f"BE50 End Total:     {e['end_total']:.2f} ({e['performance_pct']:+.2f}%)")
    print(f"Delta Equity:       {payload['equity_delta']:+.2f}")
    print(f"SL→BE: {c['SL_TO_BE']}  TP→BE: {c['TP_TO_BE']}  Ambiguous: {c['n_ambiguous']}")
    print(f"Baseline MaxDD {b['max_dd_pct']:.2f}% → BE50 {e['max_dd_pct']:.2f}%")
    print(f"Longest SL streak {b['longest_sl_streak']} → {e['longest_sl_streak']}")
    print(f"out: {payload['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
