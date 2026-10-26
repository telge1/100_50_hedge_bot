from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in (
    "phase_bt_d53_touch_connected_sl_v1",
    "phase_bt_d7_config_driven_short_pnl_integration_v1",
    "phase4d_pool_landscape_backtester_v2",
):
    sys.path.insert(0, str(ROOT / "tools" / sub))

from connectivity_component_v1 import (
    component_from_entry,
    edge_kind,
    intervals_strict_overlap,
    intervals_touch_or_overlap,
)
from strategy_rules import short_sl


def _snap(bands):
    return {
        "upper_bands": [
            {"lower_price": lo, "upper_price": hi, "origin_ids": oid} for lo, hi, oid in bands
        ]
    }


def test_edge_touch_connects_touch_mode_not_strict():
    lo_a, hi_a = 0.08408, 0.08409
    lo_b, hi_b = 0.08409, 0.08411
    assert not intervals_strict_overlap(lo_a, hi_a, lo_b, hi_b)
    assert intervals_touch_or_overlap(lo_a, hi_a, lo_b, hi_b)
    assert edge_kind(lo_a, hi_a, lo_b, hi_b) == "edge_touch_only"
    snap = _snap([(lo_a, hi_a, "a"), (lo_b, hi_b, "b")])
    s = component_from_entry(snap, entry_pool_lower=lo_a, entry_pool_upper=hi_a, mode="strict_overlap")
    t = component_from_entry(snap, entry_pool_lower=lo_a, entry_pool_upper=hi_a, mode="touch_or_overlap")
    assert s["member_count"] == 1
    assert t["member_count"] == 2
    assert t["component_upper"] == hi_b


def test_gap_never_connects():
    snap = _snap([(0.10, 0.11, "a"), (0.12, 0.13, "b")])
    t = component_from_entry(snap, entry_pool_lower=0.10, entry_pool_upper=0.11, mode="touch_or_overlap")
    assert t["member_count"] == 1


def test_sl_arithmetic():
    assert short_sl(0.08438, 0.2) == 0.08438 * 1.002


def test_touch_chain_unresolved_when_touch_only_multi():
    snap = _snap([(0.08408, 0.08409, "a"), (0.08409, 0.08411, "b"), (0.08411, 0.084112, "c")])
    t = component_from_entry(snap, entry_pool_lower=0.08408, entry_pool_upper=0.08409, mode="touch_or_overlap")
    assert t["member_count"] == 3
    assert t["status"] == "UNRESOLVED_PRICE_LEVEL_EDGE"
    assert t["unresolved_reason"] in (
        "TOUCH_ONLY_CONNECTION_CHAIN",
        "WEAK_ARTICULATION_BRIDGE_CHAIN",
    )
