"""BT-D79 pilot window inside D73 cache range."""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
D73 = ROOT / "tools" / "phase_bt_d73_selected_sep23_24_v1"
sys.path.insert(0, str(D73))

from selected_window_v1 import iter_window_closes_inclusive  # noqa: E402

PILOT_START = "2026-09-23T09:30:00Z"
PILOT_COUNT = 24


def test_pilot_closes_in_d73_window() -> None:
    all_c = iter_window_closes_inclusive()
    i = all_c.index(PILOT_START)
    chunk = all_c[i : i + PILOT_COUNT]
    assert len(chunk) == PILOT_COUNT
    assert all(c in all_c for c in chunk)
