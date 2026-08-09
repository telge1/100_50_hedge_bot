#!/usr/bin/env python3
"""July 2026 SL loser causal audit (no strategy change)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.fractal_wave_fade_loser_audit.analysis import run_analysis  # noqa: E402
from orderbook_analyse.fractal_wave_fade_loser_audit.export import write_results  # noqa: E402


def main() -> int:
    payload = run_analysis()
    paths = write_results(payload, payload["out_dir"])
    d = payload["decision"]
    a = payload["answers"]
    print()
    print("LOSER_AUDIT_READY")
    print(f"Primary Decision: {d['decision']}")
    print(f"SLs: {payload['n_sl']}  TPs: {payload['n_tp']}")
    print(f"Immediate failures: {a['q8_immediate_failures']}")
    print(f"MFE>=50% TP: {a['q9_mfe_thresholds']['mfe_ge_50pct_tp']}")
    print(f"Dominant mode: {d['dominant_failure_mode']}")
    if payload["top5"]:
        print(f"Top pattern: {payload['top5'][0]}")
    print(f"out: {payload['out_dir']}")
    for k, p in list(paths.items())[:6]:
        print(f"  {k}: {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
