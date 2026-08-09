#!/usr/bin/env python3
"""Multi-coin APT+DOGE idle-fill / overlap simulation (frozen trades)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_multicoin_overlap.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_wave_fade_multicoin_overlap.export import write_results  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force-rebuild", action="store_true")
    args = ap.parse_args(argv)

    payload = run_analysis(force_rebuild=args.force_rebuild)
    paths = write_results(payload, payload["out_dir"])
    d = payload["decision"]
    t = payload["timeline"]
    fa = payload["idle_fill_apt_by_doge"]
    fd = payload["idle_fill_doge_by_apt"]
    sa = payload["shared_apt_first"]
    m1 = payload["capital"]["M1_SINGLE_APT"]
    m2 = payload["capital"]["M2_SHARED_SLOT_APT_FIRST"]
    m3 = payload["capital"]["M3_PARALLEL_50_50"]

    print()
    print("MULTICOIN_OVERLAP_ANALYSIS_READY")
    print(f"Primary Decision: {d['decision']}")
    print()
    print(f"1. APT idle filled by DOGE: {(fa['idle_fill_ratio'] or 0)*100:.1f}%")
    print(f"2. DOGE idle filled by APT: {(fd['idle_fill_ratio'] or 0)*100:.1f}%")
    print(f"3. Both active: {t['pct_both_active']:.1f}%")
    print(f"4. Both flat: {t['pct_both_flat']:.1f}%")
    print(f"5. Extra shared-slot trades vs APT: {payload['extra_trades_shared_vs_apt']} (exec {sa['executed']})")
    print(f"6. Blocked: {sa['blocked']} ({100*(sa['block_rate'] or 0):.1f}%)")
    print(f"7. Time any position (union): {t['time_any_position_pct']:.1f}%")
    print(f"8. Net PnL M1/M2/M3: {m1['net_return_additive']:.1f} / {m2['net_return_additive']:.1f} / {m3['net_return_additive']:.1f}")
    print(f"9. PnL/day M1/M2/M3: {m1['pnl_per_day']:.4f} / {m2['pnl_per_day']:.4f} / {m3['pnl_per_day']:.4f}")
    print(f"10. PnL/cap-h M1/M2/M3: {m1['pnl_per_capital_hour']:.4f} / {m2['pnl_per_capital_hour']:.4f} / {m3['pnl_per_capital_hour']:.4f}")
    print(f"11. APT 60m coincidence: {d.get('apt_coincidence_60m_pct')}")
    print(f"12. See decision above")
    print()
    print(f"out: {payload['out_dir']}")
    for k, path in list(paths.items())[:8]:
        print(f"  {k}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
