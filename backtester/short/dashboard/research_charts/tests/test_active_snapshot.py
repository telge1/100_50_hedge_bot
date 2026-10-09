"""Unit tests for Phase 5 active snapshot fingerprints and comparisons (no CH)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_dash = Path(__file__).resolve().parents[1]
_repo = _dash.parents[2]
for p in (_repo, _dash):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.extract import (
    ActiveStateSnapshot,
    compare_active_sets,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
    active_state_fingerprint,
    deterministic_snapshot_id,
)
from research_charts.live_micro_lld_v2_ch_test.identity import ENGINE_VERSION


def _minimal_state(**overrides) -> ActiveStateSnapshot:
    base = {
        "symbol": "TUTUSDT",
        "timeframe": "5m",
        "pool_detail": 10,
        "config_hash": "abc",
        "engine_version": ENGINE_VERSION,
        "engine_input_hash": "inp",
        "candle_load_start": datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc),
        "history_from_close": datetime(2026, 9, 8, 11, 20, tzinfo=timezone.utc),
        "decision_close": datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc),
        "prefix_candle_count": 100,
        "prefix_candle_fingerprint": "fp1",
        "known_pool_count": 50,
        "active_pool_ids": ("a", "b"),
        "active_sides": ("lower", "upper"),
        "active_upper_count": 1,
        "active_lower_count": 1,
        "active_state_fingerprint": active_state_fingerprint(["a", "b"], ["lower", "upper"]),
    }
    base.update(overrides)
    return ActiveStateSnapshot(**base)


def test_active_state_fingerprint_order_invariant():
    fp1 = active_state_fingerprint(["z", "a"], ["upper", "lower"])
    fp2 = active_state_fingerprint(["a", "z"], ["lower", "upper"])
    assert fp1 == fp2


def test_deterministic_snapshot_id_stable():
    dc = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
    cls = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    kw = {
        "symbol": "TUTUSDT",
        "timeframe": "5m",
        "pool_detail": 10,
        "config_hash": "h1",
        "candle_load_start": cls,
        "decision_close": dc,
        "prefix_candle_fingerprint": "fp",
        "engine_version": ENGINE_VERSION,
    }
    assert deterministic_snapshot_id(**kw) == deterministic_snapshot_id(**kw)


def test_compare_active_sets_pass():
    st = _minimal_state()
    rep = compare_active_sets(st, list(st.active_pool_ids), list(st.active_sides))
    assert rep["pass"] is True
    assert rep["missing"] == []
    assert rep["extra"] == []


def test_compare_active_sets_detects_extra():
    st = _minimal_state()
    rep = compare_active_sets(st, ["a", "b", "c"], ["lower", "upper", "upper"])
    assert rep["pass"] is False
    assert "c" in rep["extra"]


def test_compare_active_sets_detects_side_mismatch():
    st = _minimal_state()
    rep = compare_active_sets(st, ["a", "b"], ["upper", "upper"])
    assert rep["pass"] is False
    assert rep["side_mismatch"]


def test_config_hash_isolation_in_snapshot_id():
    dc = datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)
    cls = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)
    base = {
        "symbol": "TUTUSDT",
        "timeframe": "5m",
        "pool_detail": 10,
        "candle_load_start": cls,
        "decision_close": dc,
        "prefix_candle_fingerprint": "fp",
        "engine_version": ENGINE_VERSION,
    }
    id1 = deterministic_snapshot_id(config_hash="pd10", **base)
    id2 = deterministic_snapshot_id(config_hash="pd10_box10", **base)
    assert id1 != id2
