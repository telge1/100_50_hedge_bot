"""BT-D52 safety / causal tests for price-level SL component."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for sub in (
    "phase_bt_d52_true_price_level_sl_v1",
    "phase_bt_d23_pool_first_setup_scanner_v1",
    "phase_bt_d7_config_driven_short_pnl_integration_v1",
    "phase4d_pool_landscape_backtester_v2",
):
    sys.path.insert(0, str(ROOT / "tools" / sub))

from price_level_component_v1 import component_from_entry_pool, composite_sl_from_component, intervals_overlap
from strategy_rules import short_sl


def _snap(bands):
    return {"upper_bands": [{"lower_price": lo, "upper_price": hi, "origin_ids": oid} for lo, hi, oid in bands]}


def test_entry_contained_in_component():
    snap = _snap(
        [
            (0.08408, 0.08409, "a|b"),
            (0.08409, 0.08411, "b|c"),  # overlaps first if float allows
        ]
    )
    # touch only — should NOT merge
    comp = component_from_entry_pool(snap, entry_pool_lower=0.08408, entry_pool_upper=0.08409)
    assert comp["member_count"] == 1
    assert comp["status"] == "CONFIRMED_PRICE_LEVEL_EDGE"


def test_overlap_merges_two_bands():
    snap = _snap(
        [
            (0.10, 0.12, "a"),
            (0.11, 0.13, "b"),
        ]
    )
    comp = component_from_entry_pool(snap, entry_pool_lower=0.10, entry_pool_upper=0.12)
    assert comp["member_count"] == 2
    assert comp["component_upper"] == 0.13
    sl = composite_sl_from_component(comp, sl_pct=0.2, short_sl_fn=short_sl)
    assert sl["candidate_composite_sl"] == short_sl(0.13, 0.2)


def test_no_gap_bridge():
    snap = _snap(
        [
            (0.10, 0.11, "a"),
            (0.12, 0.13, "b"),
        ]
    )
    comp = component_from_entry_pool(snap, entry_pool_lower=0.10, entry_pool_upper=0.11)
    assert comp["member_count"] == 1


def test_intervals_overlap_strict():
    assert not intervals_overlap(0.08408, 0.08409, 0.08409, 0.08411)
    assert intervals_overlap(0.10, 0.12, 0.11, 0.13)
