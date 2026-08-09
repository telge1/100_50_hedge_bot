#!/usr/bin/env python3
"""Generate manual 10-trade audit report (no strategy change)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.analysis import (  # noqa: E402
    run_manual_report,
)
from orderbook_analyse.fractal_wave_fade_manual_10_trade_report.export import (  # noqa: E402
    _fmt_ts,
)


def main() -> int:
    payload = run_manual_report()
    s = payload["summary"]
    rows = payload["rows"]
    c = s["counts"]
    v = s["verification"]

    print()
    print("MANUAL_10_TRADE_REPORT_READY")
    print(f"- window: {s['window_start']} → {s['window_end']} ({s['window_note']})")
    print(f"- trades: {s['selected_n']} (pool={s['pool_n']})")
    print(f"- LONG/SHORT: {c['long']}/{c['short']}")
    print(f"- TP/SL/other: {c['tp']}/{c['sl']}/{c['other_exit']}  upgrades={c['upgrades']}")
    print(f"- winners/losers: {c['winners']}/{c['losers']}")
    print(f"- entry verifications PASS: {v['all_entry_pass']}")
    print(f"- exit verifications PASS: {v['all_exit_pass']}")
    print(f"- accounting invariants: {s['accounting_invariants']}")
    print(f"- report: {s['paths']['md']}")
    print()
    print(f"{'#':>2} {'Time':<23} {'Sym':<10} {'Side':<5} {'Reason':<6} {'Net%':>7} {'Active':>10} {'Reserve':>8}")
    for i, r in enumerate(rows, start=1):
        print(
            f"{i:>2} {_fmt_ts(r['entry_time']):<23} {r['symbol']:<10} {r['side']:<5} "
            f"{r['exit_reason']:<6} {float(r['net_return_pct']):>+7.2f} "
            f"{float(r['active_after']):>10.2f} {float(r['reserve_after']):>8.2f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
