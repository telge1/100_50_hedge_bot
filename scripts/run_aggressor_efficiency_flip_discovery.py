#!/usr/bin/env python3
"""Run AGGRESSOR_EFFICIENCY_FLIP_DISCOVERY_V1 F0 (research-only).

Read-only ClickHouse. No dashboard/backtester/strategy changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from orderbook_analyse.aggressor_efficiency_flip.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
