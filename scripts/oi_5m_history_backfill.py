#!/usr/bin/env python3
"""CLI: Bybit REST 5m OI → ClickHouse open_interest_5m_history.

Default: dry-run. Does not touch MySQL research_open_interest_5m.
Does not start/stop collectors.

Example:
  python scripts/oi_5m_history_backfill.py \\
    --symbols BTCUSDT,DOGEUSDT \\
    --start 2026-08-18T15:10:00Z \\
    --end 2026-09-04T17:00:00Z \\
    --detect-gaps
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "dashboard"
if str(DASH) not in sys.path:
    sys.path.insert(0, str(DASH))

from collector_health.oi_backfill import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
