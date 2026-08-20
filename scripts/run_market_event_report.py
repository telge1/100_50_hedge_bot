#!/usr/bin/env python3
"""Causal market-event short report (research diagnostic, not a trading signal).

SELECT-only. Does not start/stop collectors. No strategy / retuning.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from orderbook_analyse.market_event_report.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
