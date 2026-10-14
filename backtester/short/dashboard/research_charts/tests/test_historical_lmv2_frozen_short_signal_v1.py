"""Phase 6H2 — frozen short entry on LMV2 SSOT (no trades)."""

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

from research_charts.historical_lmv2_frozen_short_paths_v1 import (
    _EXPECTED_SHA256,
    ensure_frozen_short_paths,
    frozen_entry_sha256,
    load_frozen_entry_module,
    verify_frozen_entry_hash,
)
from research_charts.historical_lmv2_frozen_short_signal_v1 import (
    FLOOR_GUARD_IN_PILOT,
    decision_close_grid,
    evaluate_decision,
)
from research_charts.historical_lmv2_ssot_legacy_bridge_v1 import bridge_member_to_legacy

UTC = timezone.utc


def test_frozen_hash_matches_manifest():
    v = verify_frozen_entry_hash()
    assert v["match"] is True
    assert v["actual_sha256"] == _EXPECTED_SHA256


def test_frozen_import_path():
    ensure_frozen_short_paths()
    fe = load_frozen_entry_module()
    assert fe.MIN_GAP_PCT == 0.8
    assert fe.MIN_BRIDGE_GAP_PCT == 0.5
    assert fe.MIN_TP_PCT == 0.8
    assert fe.STOP_PAD == 0.002
    assert fe.ATTACH_PCT == 1.0
    assert "frozen_strategies" in (fe.__file__ or "")


def test_entry_setup_requires_close_gt_open():
    fe = load_frozen_entry_module()
    moment = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    pool = {
        "pool_id": "u1",
        "side": "upper",
        "bottom": 1.0,
        "top": 1.05,
        "known": moment - timedelta(hours=1),
        "break_at": None,
        "source": moment - timedelta(hours=1),
    }
    upper2 = {
        "pool_id": "u2",
        "side": "upper",
        "bottom": 1.12,
        "top": 1.15,
        "known": moment - timedelta(hours=1),
        "break_at": None,
        "source": moment - timedelta(hours=1),
    }
    lower = {
        "pool_id": "l1",
        "side": "lower",
        "bottom": 0.85,
        "top": 0.90,
        "known": moment - timedelta(hours=1),
        "break_at": None,
        "source": moment - timedelta(hours=1),
    }
    pools = [pool, upper2, lower]
    bearish = {
        "open_time": moment - timedelta(minutes=15),
        "close_time": moment,
        "open": 0.995,
        "high": 1.01,
        "low": 0.98,
        "close": 0.99,
    }
    bullish = {**bearish, "open": 0.98, "close": 0.995}
    assert fe.entry_setup(pools, bearish, moment) is None
    assert fe.entry_setup(pools, bullish, moment) is not None


def test_attached_does_not_check_candle_color():
    fe = load_frozen_entry_module()
    pool = {"bottom": 1.0, "top": 1.1}
    bar = {"open": 0.99, "close": 0.995, "high": 1.005, "low": 0.98}
    assert fe.attached(pool, bar) is True


def test_down_4h_requires_closed_bars():
    fe = load_frozen_entry_module()
    bars = []
    t0 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    for i in range(6):
        ct = t0 + timedelta(hours=4 * (i + 1))
        bars.append(
            {
                "close_time": ct,
                "close": 90.0,
                "ema9": 95.0,
                "ema20": 98.0,
                "ema59": 99.0,
                "ema200": 100.0,
            }
        )
    assert fe.last_closed_index(bars, t0 + timedelta(hours=4)) == 0
    assert fe.down_4h(bars, 0) is False


def test_bridge_member_to_legacy_bounds():
    dc = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    leg = bridge_member_to_legacy(
        {
            "pool_id": "lldv2:DOGEUSDT:15m:micro:upper:1",
            "side": "upper",
            "lower_bound": 0.10,
            "upper_bound": 0.11,
            "known_at": "2026-09-15T09:00:00Z",
            "break_at": None,
            "confirmed_at": "2026-09-15T09:00:00Z",
        },
        decision_close=dc,
    )
    assert leg["bottom"] == 0.10
    assert leg["top"] == 0.11
    assert leg["side"] == "upper"


def test_stop_pad_computation():
    fe = load_frozen_entry_module()
    top = 0.20
    assert round(top * (1.0 + fe.STOP_PAD), 8) == round(0.20 * 1.002, 8)


def test_decision_close_grid_max_20():
    g = decision_close_grid(
        datetime(2026, 9, 15, 10, 0, tzinfo=UTC),
        datetime(2026, 9, 15, 14, 45, tzinfo=UTC),
    )
    assert len(g) == 20


@patch("research_charts.historical_lmv2_frozen_short_signal_v1.load_historical_lmv2_backtester_input_at_close")
def test_evaluate_no_signal_without_4h(mock_load):
    fe = load_frozen_entry_module()
    moment = datetime(2026, 9, 15, 10, 0, tzinfo=UTC)
    bar = {
        "open_time": moment - timedelta(minutes=15),
        "close_time": moment,
        "open": 0.10,
        "high": 0.11,
        "low": 0.09,
        "close": 0.105,
    }
    mock_state = MagicMock()
    mock_state.decision_close = "2026-09-15T10:00:00Z"
    mock_state.input_state_hash = "abc"
    mock_state.snapshot_id = "snap"
    mock_state.active_member_count = 0
    mock_state.active_members = ()
    mock_load.return_value = mock_state

    market = {"4h": {"bars": []}}
    tr = evaluate_decision(symbol="DOGEUSDT", bar=bar, market=market, pool_detail=10)
    assert tr.signal is False
    assert tr.block_reason in ("NO_ENTRY_SETUP", "DOWN_4H_FALSE")


def test_floor_guard_not_in_pilot_flag():
    assert FLOOR_GUARD_IN_PILOT is False
