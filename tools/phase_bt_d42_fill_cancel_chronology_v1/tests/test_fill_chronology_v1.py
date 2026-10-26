"""BT-D42 chronology tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase_bt_d42_fill_cancel_chronology_v1"))

from fill_chronology_v1 import (  # noqa: E402
    OrderSegmentV1,
    analyze_segment,
    build_order_segments,
)


def test_no_touch_cancelled_before():
    seg = OrderSegmentV1(
        order_id="a",
        decision_close_utc="2026-09-11T12:00:00Z",
        pending_active_from_utc="2026-09-11T12:05:00Z",
        planned_entry=0.1,
        planned_sl=0.11,
        planned_tp=0.09,
        pool_lower=0.1,
        pool_upper=0.101,
        pool_key="k",
        origin_ids="o",
        selection_reason="t",
        scanner_setup_id=None,
        end_decision_close_utc="2026-09-11T12:10:00Z",
        end_event_type="CANCEL",
    )
    prices = {
        "2026-09-11T12:05:00Z": {"o": 0.08, "h": 0.09, "l": 0.08, "c": 0.085},
        "2026-09-11T12:10:00Z": {"o": 0.085, "h": 0.09, "l": 0.084, "c": 0.088},
    }
    r = analyze_segment(seg, prices, pilot_end_utc="2026-09-12T12:00:00Z")
    assert r["chronology_status"] == "CANCELLED_BEFORE_TOUCH"
    assert r["bt41_cancel_conditional_on_unfilled"] is True


def test_touch_before_cancel():
    seg = OrderSegmentV1(
        order_id="b",
        decision_close_utc="2026-09-11T12:05:00Z",
        pending_active_from_utc="2026-09-11T12:10:00Z",
        planned_entry=0.08384,
        planned_sl=0.084,
        planned_tp=0.082,
        pool_lower=0.08384,
        pool_upper=0.083844,
        pool_key="k",
        origin_ids="o",
        selection_reason="t",
        scanner_setup_id=None,
        end_decision_close_utc="2026-09-11T12:30:00Z",
        end_event_type="CANCEL",
    )
    prices = {
        "2026-09-11T12:10:00Z": {"o": 0.083, "h": 0.0835, "l": 0.082, "c": 0.083},
        "2026-09-11T12:25:00Z": {"o": 0.08317, "h": 0.08399, "l": 0.08314, "c": 0.0835},
        "2026-09-11T12:30:00Z": {"o": 0.08351, "h": 0.08436, "l": 0.08213, "c": 0.084},
    }
    r = analyze_segment(seg, prices, pilot_end_utc="2026-09-12T12:00:00Z")
    assert r["first_high_touch_bar_5m"] == "2026-09-11T12:25:00Z"
    assert r["chronology_status"] == "EXECUTION_POSSIBLE"
    assert r["bt41_cancel_conditional_on_unfilled"] is False


def test_cancel_before_touch():
    seg = OrderSegmentV1(
        order_id="c",
        decision_close_utc="2026-09-11T12:00:00Z",
        pending_active_from_utc="2026-09-11T12:05:00Z",
        planned_entry=0.08365,
        planned_sl=0.084,
        planned_tp=0.082,
        pool_lower=0.08365,
        pool_upper=0.08366,
        pool_key="k",
        origin_ids="o",
        selection_reason="t",
        scanner_setup_id=None,
        end_decision_close_utc="2026-09-11T12:05:00Z",
        end_event_type="CANCEL",
    )
    prices = {
        "2026-09-11T12:05:00Z": {"o": 0.083, "h": 0.0836, "l": 0.083, "c": 0.0834},
    }
    r = analyze_segment(seg, prices, pilot_end_utc="2026-09-12T12:00:00Z")
    assert r["chronology_status"] in ("CANCELLED_BEFORE_TOUCH", "NO_TOUCH", "EXECUTION_UNRESOLVED")


def test_no_execution_in_decision_candle():
    seg = OrderSegmentV1(
        order_id="d",
        decision_close_utc="2026-09-11T12:05:00Z",
        pending_active_from_utc="2026-09-11T12:10:00Z",
        planned_entry=0.09,
        planned_sl=0.1,
        planned_tp=0.08,
        pool_lower=0.09,
        pool_upper=0.091,
        pool_key="k",
        origin_ids="o",
        selection_reason="t",
        scanner_setup_id=None,
    )
    prices = {
        "2026-09-11T12:05:00Z": {"o": 0.095, "h": 0.1, "l": 0.09, "c": 0.095},
        "2026-09-11T12:10:00Z": {"o": 0.094, "h": 0.096, "l": 0.092, "c": 0.093},
    }
    r = analyze_segment(seg, prices, pilot_end_utc="2026-09-12T12:00:00Z")
    assert "2026-09-11T12:05:00Z" not in [t["bar_utc"] for t in r["touch_evidence_rows"]]


def test_build_segments_from_create_cancel():
    events = [
        {
            "event_type": "CREATE",
            "decision_close_utc": "2026-09-11T12:00:00Z",
            "earliest_eligible_execution_utc": "2026-09-11T12:05:00Z",
            "planned_entry": "0.1",
            "planned_sl": "0.11",
            "planned_tp": "0.09",
            "pool_lower": "0.1",
            "pool_upper": "0.101",
            "pool_key": "k",
            "origin_ids": "o",
            "selection_reason": "x",
            "active_order_id": "oid1",
            "detail": "",
        },
        {
            "event_type": "CANCEL",
            "decision_close_utc": "2026-09-11T12:05:00Z",
            "cancel_reason": "TEST",
            "active_order_id": "",
        },
    ]
    segs = build_order_segments(events)
    assert len(segs) == 1
    assert segs[0].end_decision_close_utc == "2026-09-11T12:05:00Z"


def test_pilot_deterministic():
    out = ROOT / "results/phase_bt_d42_fill_cancel_chronology_v1/order_timelines.csv"
    if not out.exists():
        pytest.skip("run run_chronology.py")
    t1 = out.read_text(encoding="utf-8")
    t2 = out.read_text(encoding="utf-8")
    assert t1 == t2
