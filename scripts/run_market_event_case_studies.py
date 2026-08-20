#!/usr/bin/env python3
"""Auto-select market-event case studies and write diagnostic reports.

SELECT-only. No collectors. Not a trading signal.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.market_event_case_studies.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
