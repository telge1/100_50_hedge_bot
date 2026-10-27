"""BT-D67 pilot density session tests."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PKG))
sys.path.insert(0, str(ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1"))

from density_bridge_v1 import FixtureDensitySessionV1  # noqa: E402

from pilot_density_session_v1 import preflight_coverage  # noqa: E402


class _MiniPilot:
    provenance = {}

    def collect_at_close(self, bar: str) -> dict:
        if bar == "2026-01-01T12:05:00Z":
            return {"upper_bands": [{"lower_price": "0.1", "upper_price": "0.11"}], "lower_bands": []}
        raise KeyError(bar)


def test_preflight_marks_incomplete():
    rows = preflight_coverage(_MiniPilot(), ["2026-01-01T12:05:00Z", "2026-01-01T12:10:00Z"])
    assert rows[0]["coverage_status"] == "OK"
    assert rows[1]["coverage_status"] == "INCOMPLETE"
