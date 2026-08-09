#!/usr/bin/env python3
"""Drawdown distribution audit on existing BE50 full-backtest equity."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_be50_drawdown_audit.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_wave_fade_be50_drawdown_audit.export import write_results  # noqa: E402


def main() -> int:
    payload = run_analysis()
    write_results(payload, payload["out_dir"])
    print()
    print(f"Primary Decision: {payload['decision']}")
    if payload["decision"] == "DRAWDOWN_BASELINE_MISMATCH":
        print(payload.get("mismatch"))
        return 2
    st = payload["stats"]
    print(
        f"Max/2nd/3rd DD: {st['max_dd']:.2f}% / {st['second_largest_dd']:.2f}% / {st['third_largest_dd']:.2f}%"
    )
    print(
        f">=10/12/14/15%: {st['n_ge_10']} / {st['n_ge_12']} / {st['n_ge_14']} / {st['n_ge_15']}"
    )
    print(f"p95 DD: {st['p95_dd']:.2f}%")
    print(f"out: {payload['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
