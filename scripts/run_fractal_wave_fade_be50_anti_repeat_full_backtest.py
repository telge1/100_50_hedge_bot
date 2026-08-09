#!/usr/bin/env python3
"""BE50 + SAME_SIDE anti-repeat after SL vs frozen BE50 baseline."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_be50_anti_repeat_full_backtest.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_be50_anti_repeat_full_backtest.export import (  # noqa: E402
    write_results,
)


def main() -> int:
    payload = run_analysis()
    write_results(payload, payload["out_dir"])
    b, a = payload["be50_m"], payload["anti_m"]
    bs = payload["block_summary"]
    print()
    print("ANTI_REPEAT_READY")
    print(f"Primary Decision: {payload['decision']}")
    print(f"BE50 end {b['summary']['end_total']:.4g} → Anti {a['summary']['end_total']:.4g}")
    print(f"MaxDD {b['max_dd']:.2f}% → {a['max_dd']:.2f}%")
    print(
        f"TRUE SL {b['true_sl']['max_streak']}→{a['true_sl']['max_streak']} | "
        f"3+ {b['true_sl']['n_ge_3']}→{a['true_sl']['n_ge_3']} | "
        f"5+ {b['true_sl']['n_ge_5']}→{a['true_sl']['n_ge_5']}"
    )
    print(
        f">=10% DD {b['n_ge_10_dd']}→{a['n_ge_10_dd']} | "
        f"blocked={bs['n_blocked']} SL_avoided={bs['sl_avoided']} TP_lost={bs['tp_lost']}"
    )
    print(f"out: {payload['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
