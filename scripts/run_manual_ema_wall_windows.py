#!/usr/bin/env python3
"""Run manual EMA+wall window analysis (read-only research)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.l2_wall_to_wall_discovery.manual_ema_wall_windows.runner import run


def main() -> None:
    raw = ROOT / "data/orderbook_raw_shadow/ob200_v3"
    out = ROOT / "results/l2_wall_to_wall_discovery"
    run(raw_root=raw, out_root=out)


if __name__ == "__main__":
    main()
