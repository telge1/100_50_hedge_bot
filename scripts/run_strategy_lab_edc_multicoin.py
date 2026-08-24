#!/usr/bin/env python3
"""CLI: Strategy Lab EDC M0 multicoin run + export (P2D3).

Does not open ClickHouse at import time. Connection is created only in main().

Example (3-coin smoke window):

  PYTHONPATH=src python scripts/run_strategy_lab_edc_multicoin.py \\
    --strategy strategies/strategy_lab/edc_m0_strict_sync_v2.yaml \\
    --universe config/universe_tradeable_51.json \\
    --start 2026-07-24T00:00:00Z \\
    --end 2026-08-23T00:00:00Z \\
    --output-dir results/strategy_lab/edc_m0_3coin_30d_v2 \\
    --symbol XRPUSDT --symbol LITUSDT --symbol NEARUSDT
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.strategy_lab.edc_multicoin_export_v2 import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
