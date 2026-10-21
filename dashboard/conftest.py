"""Pytest path setup for LMV2 dashboard integration tests (matches _py_env() in release tests)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

DASH = Path(__file__).resolve().parent
ROOT = DASH.parent

for entry in (
    str(ROOT),
    str(DASH),
    "/home/telgenbuescher/projects/pools+ob+delta_bot",
    "/home/telgenbuescher/projects/trading_research_platform",
):
    if entry not in sys.path:
        sys.path.insert(0, entry)

os.environ.setdefault("HISTORICAL_LMV2_BATCH_READER", "1")
