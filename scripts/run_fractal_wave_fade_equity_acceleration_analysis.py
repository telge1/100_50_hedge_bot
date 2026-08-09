#!/usr/bin/env python3
"""Explain ~2024 equity acceleration of validated global-single wave-fade trades."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.export import (  # noqa: E402
    write_results,
)


def _pct(x):
    if x is None:
        return "n/a"
    return f"{100*float(x):.1f}%"


def _f(x, d=3):
    if x is None:
        return "n/a"
    return f"{float(x):.{d}f}"


def main() -> int:
    payload = run_analysis()
    paths = write_results(payload, payload["out_dir"])
    half = payload["halfyear"]
    d = payload["decision"]

    print()
    print("EQUITY_ACCELERATION_ANALYSIS_READY")
    print(f"DECISION: {d['decision']}")
    print()
    hdr = f"{'Period':<10} {'Trades/mo':>9} {'TP%':>7} {'Exp':>7} {'PF':>6} {'ATR%':>7} {'1h/4h':>7} {'Upg%':>6}"
    print(hdr)
    for _, r in half.iterrows():
        print(
            f"{r['period']:<10} {_f(r['trades_per_month'],1):>9} {_pct(r['tp_rate']):>7} "
            f"{_f(r['expectancy'],3):>7} {_f(r['profit_factor'],2):>6} "
            f"{_f(r.get('median_atr14_pct'),3):>7} {_pct(r['share_1h_4h']):>7} {_pct(r['upgrade_rate']):>6}"
        )
    print()
    print("2023 vs 2024 vs 2025")
    for y in ("2023", "2024", "2025"):
        sub = half[half["period"].str.startswith(y)]
        if sub.empty:
            continue
        print(
            f"  {y}: trades/mo={_f(sub['trades_per_month'].mean(),1)} "
            f"TP%={_pct(sub['tp_rate'].mean())} "
            f"exp={_f(sub['expectancy'].mean(),3)} "
            f"PF={_f(sub['profit_factor'].mean(),2)} "
            f"ATR%={_f(sub['median_atr14_pct'].mean(),3)} "
            f"1h/4h={_pct(sub['share_1h_4h'].mean())} "
            f"upg={_pct(sub['upgrade_rate'].mean())} "
            f"cum={_f(sub['cumulative_additive_return'].sum(),1)}"
        )
    print()
    print("Strongest shifts (2024 vs 2023):")
    dr = d["drivers"]
    print(f"  trades/mo Δ%: {dr['A_MORE_TRADES'].get('delta_2024_vs_2023_pct')}")
    print(f"  win_rate Δpp: {dr['B_HIGHER_WIN_RATE'].get('delta_pp_2024_vs_2023')}")
    print(f"  expectancy 2023→2024: {dr['C_LARGER_WINNERS'].get('expectancy_2023')} → {dr['C_LARGER_WINNERS'].get('expectancy_2024')}")
    print(f"  ATR% Δ%: {dr['E_HIGHER_MARKET_VOLATILITY'].get('atr_delta_pct_2024_vs_2023')}")
    print(f"  1h/4h share: {dr['D_BETTER_TF_MIX_MORE_UPGRADES'].get('share_1h_4h_2023')} → {dr['D_BETTER_TF_MIX_MORE_UPGRADES'].get('share_1h_4h_2024')}")
    print(f"  upgrade rate: {dr['D_BETTER_TF_MIX_MORE_UPGRADES'].get('upgrade_rate_2023')} → {dr['D_BETTER_TF_MIX_MORE_UPGRADES'].get('upgrade_rate_2024')}")
    print(f"  symbols: APT={d['apt_accelerates']} DOGE={d['doge_accelerates']} both={d['both_symbols_accelerate']}")
    print()
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
