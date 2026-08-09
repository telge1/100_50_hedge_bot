#!/usr/bin/env python3
"""Higher-TF Stoch context analysis for validated global-single trades."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context.analysis import (  # noqa: E402
    run_analysis,
)
from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context.export import (  # noqa: E402
    write_results,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "results" / "fractal_wave_fade_higher_tf_stoch_context",
    )
    args = p.parse_args(argv)
    payload = run_analysis()
    paths = write_results(payload, args.out_dir)
    d = payload["decisions"]
    print(f"[primary] {d['primary']}", flush=True)
    print(f"[secondary] {d['30m']} | {d['1h']} | {d['4h']}", flush=True)
    print(f"[alignment] {d['alignment']}", flush=True)
    print(f"[without_support] {d['without_support']}", flush=True)
    print(f"[role] {payload['answers'].get('q10_role')}", flush=True)
    b = payload["baseline"]
    print(
        f"[baseline] n={payload['n_trades']} tp_rate={b.get('tp_rate')} "
        f"exp={b.get('expectancy')} PF={b.get('profit_factor')}",
        flush=True,
    )
    for k, path in paths.items():
        print(f"  {k}: {path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
