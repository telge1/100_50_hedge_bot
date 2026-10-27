from __future__ import annotations

import sys
from pathlib import Path

PKG = Path(__file__).resolve().parents[1]
if str(PKG) not in sys.path:
    sys.path.insert(0, str(PKG))

from selected_window_v1 import EXPECTED_CLOSE_COUNT, iter_window_closes_inclusive


def test_window_close_count() -> None:
    closes = iter_window_closes_inclusive()
    assert len(closes) == EXPECTED_CLOSE_COUNT == 386
    assert closes[0] == "2026-09-23T06:45:00Z"
    assert closes[-1] == "2026-09-24T14:50:00Z"
