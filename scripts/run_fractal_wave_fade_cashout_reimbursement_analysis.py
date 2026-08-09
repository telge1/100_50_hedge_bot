#!/usr/bin/env python3
"""Cashout + loss-reimbursement analysis on validated global-single trades."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_wave_fade_cashout_reimbursement_analysis",
    )
    args = p.parse_args(argv)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    c = payload["controls"]
    print(
        f"[done] n={c['n_trades']} maxSL={c['max_consecutive_sl']} "
        f"maxLose={c['max_consecutive_losing_trades']} parity={payload['parity_0pct']}",
        flush=True,
    )
    m = payload["matrix"]
    prim = m[(m["coverage_rate_pct"] == 100) & (m["reimburse_mode"] == "ALL_NEGATIVE")]
    for _, r in prim.sort_values("cashout_rate_pct").iterrows():
        print(
            f"  {int(r['cashout_rate_pct']):2d}%/100%  active={r['end_active']:.4g} "
            f"reserve={r['end_reserve']:.4g} total={r['end_total_wealth']:.4g} "
            f"actDD={r['active_max_dd_pct']:.2f}% totDD={r['total_max_dd_pct']:.2f}% "
            f"full={r['fully_reimbursed']} part={r['partially_reimbursed']} "
            f"zero={r['reserve_hit_zero_events']}",
            flush=True,
        )
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
