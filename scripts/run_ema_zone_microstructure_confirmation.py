#!/usr/bin/env python3
"""Run EMA Zone Microstructure Confirmation candidate detector (research/read-only).

Uses compile_candidate_discovery_v2 — never the trade-backtest compiler.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.ema_zone_microstructure_confirmation.runner import run


def main() -> None:
    raw = ROOT / "data/orderbook_raw_shadow/ob200_v3"
    out = ROOT / "results/ema_zone_microstructure_confirmation"
    run(repo_root=ROOT, raw_root=raw, out_root=out)


if __name__ == "__main__":
    main()
