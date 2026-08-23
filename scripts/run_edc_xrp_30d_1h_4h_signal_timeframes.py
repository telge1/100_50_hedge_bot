#!/usr/bin/env python3
"""CLI: XRP 30d 1h/4h signal timeframe research (M0/M4/M5)."""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.ema_dual_cross_multisource.tolerance_research.xrp_30d_1h_4h_signal_timeframes_runner import (  # noqa: E402
    run_xrp_30d_1h_4h_signal_timeframes,
)


def main() -> None:
    result = run_xrp_30d_1h_4h_signal_timeframes()
    print(f"export_dir: {result['export_dir']}")
    print(f"verdict: {result['verdict']}")


if __name__ == "__main__":
    main()
