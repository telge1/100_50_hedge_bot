"""PYTHONPATH / env for published SSOT landscape window (read-only)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPREAD_DASH = Path("/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/dashboard")
TRP = Path("/home/telgenbuescher/projects/trading_research_platform")


def ensure_ssot_import_path() -> Path:
    os.environ.setdefault("HISTORICAL_LMV2_BATCH_READER", "1")
    for p in (str(TRP), str(ROOT), str(SPREAD_DASH.parent), str(SPREAD_DASH)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from research_charts.trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    return SPREAD_DASH
