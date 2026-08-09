#!/usr/bin/env python3
"""Independent double-check of global single-position wave-fade backtest."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_global_single_position_doublecheck.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_wave_fade_global_single_position_doublecheck",
    )
    args = p.parse_args(argv)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    print(f"[primary] {payload['primary_decision']}", flush=True)
    for k, v in payload["secondary"].items():
        print(f"  {k}: {v}", flush=True)
    c = payload["counts"]
    print(
        f"[counts] trades={c['trades_checked']} lookahead={c['lookahead_violations']} "
        f"entry_mis={c['entry_timestamp_mismatch_count']}/{c['entry_price_mismatch_count']} "
        f"exit_mis={c['exit_time_mismatch_count']}/{c['exit_reason_mismatch_count']} "
        f"overlap={c['overlapping_trade_count']} fee_mis={c['fee_mismatch_count']}",
        flush=True,
    )
    print(f"[credible] {payload['credible']}", flush=True)
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
