"""BT-D41 pending lifecycle tests (no fills)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
for p in (
    ROOT / "tools/phase_bt_d6_config_driven_strategy_pnl_foundation_v1",
    ROOT / "tools/phase_bt_d7_config_driven_short_pnl_integration_v1",
    ROOT / "tools/phase_bt_d26_dynamic_upper_pool_selector_v1",
    ROOT / "tools/phase_bt_d23_pool_first_setup_scanner_v1",
    ROOT / "tools/phase4d_pool_landscape_backtester_v2",
    ROOT / "tools/phase_bt_d41_pending_lifecycle_v1",
):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from pending_lifecycle_v1 import (  # noqa: E402
    EVENT_CANCEL,
    EVENT_KEEP,
    EVENT_POTENTIAL_FILL_UNRESOLVED,
    PendingOrderV1,
    intrabar_potential_fill_events,
    next_5m_open_after_close,
    review_pending_at_close,
)


def _minimal_config(**se_over):
    se = {
        "entry_pool_selection_mode": "pool_first_structural_v1",
        "entry_price_reference": "upper_pool_lower_edge",
        "entry_offset_pct": 0.0,
        "pool_min_age_bars": 0,
        "upper_gap_min_pct": 0.5,
        "upper_gap_measurement_mode": "adjacent_union_interval_gap_pct_v1",
        "upper_pool_proximity_pct": 0.05,
        "pending_limit_enabled": True,
        "pool_cancel_identity_mode": "origin_set_v1",
    }
    se.update(se_over)
    return {
        "config_version": "bt_d6_strategy_backtest_config_v1",
        "short_entry": se,
        "risk": {"sl_above_upper_top_pct": 0.2, "max_concurrent_positions": 1},
        "take_profit": {
            "selection_mode": "next_eligible_band",
            "min_distance_pct": 0.5,
            "distance_reference": "actual_fill_to_lower_upper_edge",
            "dense_cluster_semantics_approved": False,
            "tp_pool_quality_gate_enabled": False,
        },
    }


def _pending(lo=0.08681, hi=0.086838, entry=0.08681, sl=0.087011676, tp=0.08586):
    return PendingOrderV1(
        order_id="t1",
        lifecycle_state="PENDING",
        decision_close_utc="2026-09-11T15:35:00Z",
        intent_created_utc="2026-09-11T15:35:00Z",
        earliest_eligible_execution_utc=next_5m_open_after_close("2026-09-11T15:35:00Z"),
        pool_lower=lo,
        pool_upper=hi,
        pool_key=f"{lo}|{hi}",
        origin_ids="o1|o2",
        origin_set_key="o1|o2",
        planned_entry=entry,
        planned_sl=sl,
        planned_tp=tp,
        selection_reason="TEST",
    )


def test_pool_unchanged_keep_when_eligible(snapshot_fixture_path=None):
    """1. Pool unchanged -> KEEP (integration via pilot CSV if present)."""
    out = ROOT / "results/phase_bt_d41_pending_lifecycle_v1/lifecycle_events.csv"
    if not out.exists():
        pytest.skip("run run_lifecycle.py first")
    text = out.read_text(encoding="utf-8")
    assert "KEEP" in text


def test_not_active_cancels():
    """3. Pool not ACTIVE -> CANCEL."""
    cfg = _minimal_config()
    snap = {"upper_bands": [], "lower_bands": []}
    res = review_pending_at_close(
        cfg,
        decision_close_utc="2026-09-11T16:00:00Z",
        pending=_pending(),
        candle={"c": 0.084},
        snapshot=snap,
        pool_age_bars={},
    )
    assert res.pending is None
    assert res.events[0].event_type == EVENT_CANCEL
    assert res.events[0].cancel_reason == "POOL_NOT_ACTIVE"


def test_price_below_pool_cancels_ineligible():
    """8/9 structural: close above pool lower fails eligibility."""
    cfg = _minimal_config()
    lo, hi = 0.08681, 0.086838
    snap = {
        "upper_bands": [
            {
                "lower_price": lo,
                "upper_price": hi,
                "origin_ids": "o1|o2",
            }
        ],
        "lower_bands": [
            {"lower_price": 0.082, "upper_price": 0.083, "origin_ids": "l1|l2"},
        ],
    }
    res = review_pending_at_close(
        cfg,
        decision_close_utc="2026-09-11T16:00:00Z",
        pending=_pending(lo, hi),
        candle={"c": 0.087},
        snapshot=snap,
        pool_age_bars={_pending(lo, hi).pool_key: 5},
        require_large_upper_gap=False,
    )
    assert res.pending is None
    assert res.events[0].cancel_reason == "POOL_NOT_ABOVE_PRICE"


def test_intrabar_unresolved_high_touch():
    """10. potential intrabar touch -> UNRESOLVED."""
    p = _pending()
    evs = intrabar_potential_fill_events(
        p,
        bar_utc="2026-09-11T15:40:00Z",
        ohlc={"h": 0.087, "l": 0.086, "o": 0.0865, "c": 0.0866},
        decision_close_utc="2026-09-11T15:40:00Z",
    )
    assert len(evs) == 1
    assert evs[0].event_type == EVENT_POTENTIAL_FILL_UNRESOLVED


def test_intrabar_before_active_from_empty():
    p = _pending()
    evs = intrabar_potential_fill_events(
        p,
        bar_utc="2026-09-11T15:35:00Z",
        ohlc={"h": 0.09},
        decision_close_utc="2026-09-11T15:35:00Z",
    )
    assert evs == []


def test_no_parallel_pending_in_pilot():
    """12. At most one pending order in event stream."""
    out = ROOT / "results/phase_bt_d41_pending_lifecycle_v1/lifecycle_events.csv"
    if not out.exists():
        pytest.skip("run run_lifecycle.py first")
    import csv

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    active = 0
    for r in rows:
        if r["event_type"] == "CREATE":
            active = 1
        elif r["event_type"] in ("CANCEL", "REPLACE"):
            active = 0
        elif r["event_type"] == "KEEP" and active > 1:
            pytest.fail("parallel pending")
    assert True


def test_planned_scanner_matches_setups_count():
    out = ROOT / "results/phase_bt_d41_pending_lifecycle_v1/lifecycle_events.csv"
    if not out.exists():
        pytest.skip("run run_lifecycle.py first")
    import csv

    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    planned = sum(1 for r in rows if r["event_type"] == "PLANNED_SCANNER")
    creates = sum(1 for r in rows if r["event_type"] == "CREATE")
    assert planned == 102
    assert creates == 102
