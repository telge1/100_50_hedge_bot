"""Phase 6H3 — SSOT floor guard + frozen entry (no trades)."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_repo = Path(__file__).resolve().parents[5]
_dash = _repo / "backtester" / "short"
for p in (str(_repo), str(_dash)):
    if p not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.historical_lmv2_frozen_short_paths_v1 import verify_frozen_entry_hash
from research_charts.historical_lmv2_ssot_floor_guard_v1 import guard_contract
from research_charts.historical_lmv2_ssot_four_h_pools_v1 import (
    FOUR_H_PIPELINE_LINE_ID,
    four_h_config_hash,
    inventory_four_h_source,
)


def test_frozen_hash_still_matches():
    assert verify_frozen_entry_hash()["match"] is True


def test_guard_contract_has_spec():
    c = guard_contract()
    assert c["spec"]["episode_hours"] == 48
    assert c["four_h_pool_source"]


def test_four_h_line_id_constant():
    assert "4hpd10" in FOUR_H_PIPELINE_LINE_ID


@patch("research_charts.historical_lmv2_ssot_floor_guard_v1.load_ssot_legacy_pools_4h_at_close")
def test_ssot_guard_snapshot_uses_lowers(mock_load):
    from research_charts.historical_lmv2.strategy_paths import ensure_strategy_paths
    from research_charts.historical_lmv2_ssot_floor_guard_v1 import SsotH4FloorGuard

    ensure_strategy_paths()
    moment = datetime(2026, 6, 3, 11, 15, tzinfo=timezone.utc)
    mock_load.return_value = (
        [
            {
                "pool_id": "lldv2:DOGEUSDT:4h:micro:lower:1",
                "side": "lower",
                "bottom": 0.08,
                "top": 0.085,
                "known": moment - timedelta(hours=8),
                "break_at": None,
                "source": moment - timedelta(hours=8),
            }
        ],
        {},
    )
    g = SsotH4FloorGuard(
        "DOGEUSDT",
        datetime(2026, 2, 10, tzinfo=timezone.utc),
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        four_h_bar_closes=[datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc)],
    )
    snap = g._snapshot(moment)
    assert snap.deepest_active is not None


def test_e1r_not_in_guard_contract():
    c = guard_contract()
    assert "e1r" not in str(c).lower() or "not" in c.get("freeze_note", "").lower()
