"""Unit tests for wall decision metrics helpers (no CH required for pure paths)."""

from __future__ import annotations

import sys
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parents[1]
if str(DASHBOARD) not in sys.path:
    sys.path.insert(0, str(DASHBOARD))

from wall_decision_v1.metrics import _match_wall, _zone  # noqa: E402


def test_match_wall_by_side_and_price():
    bars = [
        {"side": "ASK", "price": 101100.0, "qty": 10},
        {"side": "BID", "price": 100900.0, "qty": 8},
    ]
    hit = _match_wall(bars, {"side": "ASK", "price": 101100.0}, tick=0.1)
    assert hit is not None
    assert hit["qty"] == 10
    assert _match_wall(bars, {"side": "ASK", "price": 999999.0}, tick=0.1) is None


def test_zone_falls_back_to_tick_band():
    lo, hi = _zone({"price": 100.0}, tick=0.1)
    assert lo < 100.0 < hi
