"""Tests for C3.5 simple path audit (research-only)."""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from research.regime_scanner.pullback_entry_c3_5 import apply_pullback_entry, PullbackEntryConfig
from research.regime_scanner.pullback_entry_c3_5_simple_path_audit import (
    build_cases,
    collect_filled_entries,
    measure_path_moves,
)


def test_short_formulas() -> None:
    # Entry 100. Path: dip to 97, spike to 103, then later low 98
    highs = np.array([101.0, 103.0, 102.0, 100.0, 99.0, 99.5])
    lows = np.array([99.0, 100.0, 98.0, 97.0, 96.5, 98.0])
    # Wait - with-signal lowest is 96.5 at idx 4; against highest 103 at idx 1
    # after against from idx 1: lows [100, 98, 97, 96.5, 98] -> min 96.5 at later
    ts = list(range(6))
    m = measure_path_moves(
        side=-1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=ts,
        fill_bar=0,
        horizon_bars=6,
        n_bars=6,
    )
    assert abs(m["max_down_below_entry_pct"] - 3.5) < 1e-9  # 100-96.5
    assert abs(m["max_against_signal_pct"] - 3.0) < 1e-9  # 103-100
    assert m["max_against_signal_bars_from_entry"] == 1
    assert m["max_down_below_entry_bars_from_entry"] == 4
    # later low from against bar 1: min of lows[1:]=96.5 at local 4 -> bars_from_against=3
    assert abs(m["after_against_max_below_entry_pct"] - 3.5) < 1e-9
    assert m["after_against_max_below_entry_bars_from_against"] == 3
    assert m["reclaimed_entry_after_against"] is True


def test_long_mirrored() -> None:
    highs = np.array([101.0, 100.0, 102.0, 104.0, 103.0, 103.5])
    lows = np.array([99.0, 97.0, 98.0, 100.0, 101.0, 100.5])
    ts = list(range(6))
    m = measure_path_moves(
        side=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=ts,
        fill_bar=0,
        horizon_bars=6,
        n_bars=6,
    )
    assert abs(m["max_up_above_entry_pct"] - 4.0) < 1e-9  # 104
    assert abs(m["max_against_signal_pct"] - 3.0) < 1e-9  # 100-97
    assert m["max_against_signal_bars_from_entry"] == 1
    # from against idx 1 highs [100,102,104,103,103.5] -> max 104 at local 3, bars_from_against=2
    assert abs(m["after_against_max_above_entry_pct"] - 4.0) < 1e-9
    assert m["after_against_max_above_entry_bars_from_against"] == 2
    assert m["reclaimed_entry_after_against"] is True


def test_point3_ignores_data_before_against() -> None:
    # Short: early low 90 before against high; after against only lows >= 99
    highs = np.array([100.5, 105.0, 104.0, 103.0])
    lows = np.array([90.0, 99.0, 99.5, 99.2])
    m = measure_path_moves(
        side=-1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0, 1, 2, 3],
        fill_bar=0,
        horizon_bars=4,
        n_bars=4,
    )
    assert m["max_against_signal_bars_from_entry"] == 1
    # Must NOT use 90 for after_against
    assert m["after_against_max_below_entry_price"] == 99.0
    assert abs(m["after_against_max_below_entry_pct"] - 1.0) < 1e-9
    assert m["max_down_below_entry_price"] == 90.0  # with-signal may use early low


def test_tie_uses_first_timestamp() -> None:
    highs = np.array([102.0, 102.0, 101.0])
    lows = np.array([99.0, 98.0, 98.0])
    m = measure_path_moves(
        side=-1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=["a", "b", "c"],
        fill_bar=0,
        horizon_bars=3,
        n_bars=3,
    )
    assert m["max_against_signal_bars_from_entry"] == 0  # first 102
    assert m["max_against_signal_timestamp"] == "a"
    # later low from 0: first 98 at idx 1
    assert m["after_against_max_below_entry_bars_from_entry"] == 1
    assert m["after_against_max_below_entry_timestamp"] == "b"


def test_incomplete_horizon_flag() -> None:
    highs = np.array([101.0, 102.0])
    lows = np.array([99.0, 98.0])
    m = measure_path_moves(
        side=-1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[0, 1],
        fill_bar=0,
        horizon_bars=6,
        n_bars=2,
    )
    assert m["incomplete_horizon"] is True
    assert m["valid"] is True
    assert m["max_down_below_entry_price"] == 98.0


def test_trigger_without_next_open_ignored() -> None:
    filled = collect_filled_entries(
        [
            {"bar_index": 9, "side": -1, "entry_price": 100.0, "timestamp": "t"},
        ],
        n_bars=10,
    )
    assert filled == []  # fill would be bar 10, out of range


def test_annulled_setups_not_in_entries() -> None:
    """SM entries list only real fills; path cases come only from those fills."""
    rows = []
    for i in range(20):
        rows.append(
            {
                "bar_index": i,
                "timestamp": pd.Timestamp("2026-02-01", tz="UTC") + pd.Timedelta(minutes=5 * i),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "atr_14": 1.0,
                "ema_9": 99.0,
                "ema_20": 100.0,
                "ema_50": 101.0,
                "adx": 20.0,
                "plus_di": 10.0,
                "minus_di": 25.0,
                "ema_9_slope_3": -0.1,
                "ema_20_slope_3": -0.05,
                "adx_rising_2": True,
                "adx_rising_3": True,
                "ema9_below_ema20": True,
                "ema9_above_ema20": False,
                "ema20_below_ema50": True,
                "ema_cross_age": 5,
                "arm_edge_external_bear": i == 2,
                "arm_edge_external_bull": False,
                "arm_edge_internal_bear": False,
                "arm_edge_internal_bull": False,
                "arm_edge_choch_bear": False,
                "arm_edge_choch_bull": False,
                "arm_edge_major_bear": False,
                "arm_edge_major_bull": False,
                "arm_edge_struct_prot_bear": False,
                "arm_edge_struct_prot_bull": False,
                "micro_swing_high": 102.0,
                "micro_swing_low": 98.0,
                "protected_high": 103.0,
                "protected_low": 97.0,
                "protected_structure_state": "x",
                "major_direction": -1,
                "m15_major_direction": 0,
                "m30_major_direction": 0,
                "m15_protected_structure_state": "",
                "m30_protected_structure_state": "",
            }
        )
    frame = pd.DataFrame(rows)
    cfg = PullbackEntryConfig(name="A1")
    _tl, entries, lives = apply_pullback_entry(frame, cfg, return_lifecycles=True)
    filled = collect_filled_entries(entries, len(frame))
    cases = build_cases(frame, entries, symbol="T", variant="A1", horizons=(6,))
    assert len(cases) == len(filled)
    # No path row for setups that never entered
    non_entered = {int(x["setup_id"]) for x in lives if not x.get("entry_created")}
    if not cases.empty and cases["setup_id"].notna().any():
        case_ids = set(cases["setup_id"].dropna().astype(int))
        assert case_ids.isdisjoint(non_entered)


def test_no_lookahead_in_module_source() -> None:
    import research.regime_scanner.pullback_entry_c3_5_simple_path_audit as mod

    src = inspect.getsource(mod)
    assert "lookahead_on" not in src
    assert "shift(-" not in src


def test_bars_distances() -> None:
    highs = np.array([100.0, 101.0, 106.0, 105.0])
    lows = np.array([99.0, 98.5, 97.0, 96.0])
    m = measure_path_moves(
        side=-1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        timestamps=[10, 11, 12, 13],
        fill_bar=0,
        horizon_bars=4,
        n_bars=4,
    )
    assert m["max_against_signal_bars_from_entry"] == 2
    assert m["max_down_below_entry_bars_from_entry"] == 3
    assert m["after_against_max_below_entry_bars_from_entry"] == 3
    assert m["after_against_max_below_entry_bars_from_against"] == 1
