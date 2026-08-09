#!/usr/bin/env python3
"""Generate Active + Reserve equity curves with leverage variants."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_equity_curve_analysis.analysis import (  # noqa: E402
    run_analysis,
)


def _fmt_money(x: float) -> str:
    if abs(x) >= 1e6:
        return f"{x:.4g}"
    return f"{x:.2f}"


def main() -> int:
    payload = run_analysis()
    out = payload["out_dir"]
    print()
    print("EQUITY_AND_RESERVE_CURVES_READY")
    print(f"- out: {out}")
    print()

    for s in payload["summaries"]:
        lev = int(s["leverage"])
        print(f"=== {lev}x ===")
        if s["capital_depleted"]:
            print("CAPITAL_DEPLETED")
            print(f"  depleted_at: {s['depleted_at_time']}  trade_id={s['depleted_at_trade_id']}")
        print(f"  Start Active:     {_fmt_money(s['start_active'])}")
        print(f"  End Active:       {_fmt_money(s['end_active'])}")
        print(f"  Max Active:       {_fmt_money(s['max_active'])}")
        print(f"  Min Active:       {_fmt_money(s['min_active'])}")
        print(f"  Active MaxDD:     {s['active_max_dd_pct']:.2f}%  @ {s['active_max_dd_time']}")
        print(f"  Total Cashout:    {_fmt_money(s['total_cashout_generated'])}")
        print(f"  Total Reimburse:  {_fmt_money(s['total_reimbursement_used'])}")
        print(f"  End Reserve:      {_fmt_money(s['end_reserve'])}")
        print(f"  Max Reserve:      {_fmt_money(s['max_reserve'])}  @ {s['max_reserve_time']}")
        print(f"  End Total Wealth: {_fmt_money(s['end_total_wealth'])}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
