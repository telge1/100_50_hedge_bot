#!/usr/bin/env python3
"""Full-history BE50 vs baseline A/B on fractal wave-fade trades."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_be50_full_backtest.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_wave_fade_be50_full_backtest.export import write_results  # noqa: E402


def main() -> int:
    payload = run_analysis()
    write_results(payload, payload["out_dir"])
    if payload.get("baseline_reproduction_failed"):
        print("BASELINE_REPRODUCTION_FAILED")
        return 2
    b, e = payload["base_summary"], payload["be_summary"]
    c = payload["counts"]
    tb, te = payload["true_sl_base"], payload["true_sl_be"]
    print()
    print("BE50_FULL_BACKTEST_READY")
    print(f"Primary Decision: {payload['decision']}")
    print(f"Baseline End: {b['end_total']:.4g} ({b['performance_pct']:+.4g}%)")
    print(f"BE50 End:     {e['end_total']:.4g} ({e['performance_pct']:+.4g}%)")
    print(f"Delta Equity: {payload['equity_delta']:+.4g}")
    print(f"MaxDD {b['max_dd_pct']:.2f}% → {e['max_dd_pct']:.2f}%")
    print(f"TRUE SL streak {tb['max_streak']} → {te['max_streak']} | 3+: {tb['n_ge_3']}→{te['n_ge_3']} | 5+: {tb['n_ge_5']}→{te['n_ge_5']}")
    print(f"SL→BE {c['SL_TO_BE']}  TP→BE {c['TP_TO_BE']}  Ambiguous {c['n_ambiguous']}")
    print(f"out: {payload['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
