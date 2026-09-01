"""Unit tests for frozen W1–W4 and R0–R9. No outcome peeking."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.stoch_fade_filter_tests.combined_entry_warnings.warnings import (
    apply_rules,
    overlap_flags_for_symbol,
    pre_entry_progress,
    w1_5m_exhausted_in_trade_direction,
    w2_1m_turning_against_trade,
    w3_pre_entry_tp_progress_ge_25pct,
    warning_score,
)
from research.stoch_fade_filter_tests.zec_5m_exhaustion.forward import trade_forward_paths
from research.stoch_fade_filter_tests.zec_5m_exhaustion.rule import stoch_exhausted_in_trade_direction


def test_w1_matches_previous_5m_definition():
    for direction, k in [("LONG", 80.1), ("LONG", 80.0), ("SHORT", 19.9), ("SHORT", 20.0), ("LONG", None)]:
        assert w1_5m_exhausted_in_trade_direction(direction, k) == stoch_exhausted_in_trade_direction(direction, k)


def test_w2_long_short_mirror():
    long = w2_1m_turning_against_trade(
        direction="LONG", k=70, d=80, k_prev=90, d_prev=85, cross_up=False, cross_down=True, phase="BULL_MOMENTUM"
    )
    short = w2_1m_turning_against_trade(
        direction="SHORT", k=30, d=20, k_prev=10, d_prev=15, cross_up=True, cross_down=False, phase="BEAR_MOMENTUM"
    )
    assert long["w2_1m_turning_against_trade"] is True
    assert short["w2_1m_turning_against_trade"] is True
    assert long["w2_cross_against"] is True
    assert short["w2_cross_against"] is True


def test_w2_spread_and_phase():
    long_spread = w2_1m_turning_against_trade(
        direction="LONG", k=40, d=50, k_prev=48, d_prev=49, cross_up=False, cross_down=False, phase="NEUTRAL"
    )
    assert long_spread["w2_spread_against"] is True
    long_phase = w2_1m_turning_against_trade(
        direction="LONG", k=85, d=90, k_prev=80, d_prev=82, cross_up=False, cross_down=False, phase="OVERBOUGHT_TURNING_DOWN"
    )
    assert long_phase["w2_phase_against"] is True
    short_phase = w2_1m_turning_against_trade(
        direction="SHORT", k=10, d=8, k_prev=12, d_prev=15, cross_up=False, cross_down=False, phase="OVERSOLD_TURNING_UP"
    )
    assert short_phase["w2_phase_against"] is True


def test_w2_missing_not_cast_to_false():
    miss = w2_1m_turning_against_trade(
        direction="LONG", k=None, d=None, k_prev=None, d_prev=None, cross_up=None, cross_down=None, phase=None
    )
    assert miss["w2_1m_turning_against_trade"] is None
    assert miss["w2_missing"] is True


def test_w3_long_short_direction():
    long = pre_entry_progress(direction="LONG", entry_price=102, wave_end_price=100, tp_price=104)
    short = pre_entry_progress(direction="SHORT", entry_price=98, wave_end_price=100, tp_price=96)
    assert abs(long["pre_entry_progress"] - 0.5) < 1e-12
    assert abs(short["pre_entry_progress"] - 0.5) < 1e-12
    assert w3_pre_entry_tp_progress_ge_25pct(0.25) is True
    assert w3_pre_entry_tp_progress_ge_25pct(0.249) is False
    bad = pre_entry_progress(direction="LONG", entry_price=100, wave_end_price=100, tp_price=100)
    assert bad["pre_entry_progress_missing"] is True
    assert w3_pre_entry_tp_progress_ge_25pct(None) is None


def test_w4_interval_and_open():
    frame = pd.DataFrame(
        {
            "symbol": ["ZECUSDT"] * 3,
            "signal_id": ["a", "b", "c"],
            "direction": ["SHORT", "LONG", "SHORT"],
            "entry_time": pd.to_datetime(["2026-08-16T05:00:00Z", "2026-08-16T06:00:00Z", "2026-08-16T07:00:00Z"], utc=True),
            "exit_time": pd.to_datetime(["2026-08-16T08:00:00Z", "2026-08-16T06:30:00Z", None], utc=True),
            "is_open": [False, False, True],
            "outcome": ["LOSS", "WIN", "OPEN"],
        }
    )
    out = overlap_flags_for_symbol(frame)
    assert list(out["w4_symbol_trade_already_open"]) == [False, True, True]
    assert bool(out.loc[1, "w4_overlap_opposite_direction"]) is True
    # equal entry does not count as previous < current
    same = pd.DataFrame(
        {
            "symbol": ["ZECUSDT", "ZECUSDT"],
            "signal_id": ["x", "y"],
            "direction": ["LONG", "SHORT"],
            "entry_time": pd.to_datetime(["2026-08-16T09:00:00Z", "2026-08-16T09:00:00Z"], utc=True),
            "exit_time": pd.to_datetime([None, None], utc=True),
            "is_open": [True, True],
            "outcome": ["OPEN", "OPEN"],
        }
    )
    same_o = overlap_flags_for_symbol(same)
    assert list(same_o["w4_symbol_trade_already_open"]) == [False, False]


def test_missing_not_zero_in_score_or_and_rules():
    sc = warning_score(True, None, False, False)
    assert sc["warning_score_true"] == 1
    assert sc["warning_score_missing"] == 1
    assert sc["warning_score_complete"] is None
    rules = apply_rules(True, None, False, False)
    assert rules["R1"] is True
    assert rules["R2"] is False
    assert rules["R4"] is False


def test_r2_r3_use_known_trues_only():
    rules = apply_rules(True, True, None, False)
    assert rules["R2"] is True
    assert rules["R3"] is False
    rules3 = apply_rules(True, True, True, False)
    assert rules3["R3"] is True
    assert apply_rules(False, False, False, False)["R0"] is False


def test_forward_path_does_not_change_outcome():
    t0 = np.datetime64("2026-08-16T09:46:00")
    times = t0 + np.arange(400).astype("timedelta64[m]")
    paths = trade_forward_paths(
        direction="SHORT",
        entry_price=100.0,
        tp_price=99.0,
        sl_price=101.5,
        entry_time=times[0],
        exit_time=times[10],
        exit_reason="SL",
        times=times,
        open_=np.full(400, 100.0),
        high=np.full(400, 100.2),
        low=np.full(400, 99.8),
    )
    high = np.full(400, 100.2)
    high[10] = 102.0
    paths = trade_forward_paths(
        direction="SHORT",
        entry_price=100.0,
        tp_price=99.0,
        sl_price=101.5,
        entry_time=times[0],
        exit_time=times[10],
        exit_reason="SL",
        times=times,
        open_=np.full(400, 99.0),
        high=high,
        low=np.full(400, 98.5),
    )
    assert paths["original_exit_reason"] == "SL"
    assert paths["4h_still_open"] is False


def test_fees_constant():
    from research.stoch_fade_filter_tests.combined_entry_warnings.config import FEE_PP

    assert FEE_PP == 0.11


def test_assign_split_counts():
    from research.stoch_fade_trade_context_analysis.pipeline import assign_split

    n = 1158
    frame = pd.DataFrame(
        {
            "signal_id": [f"s{i:04d}" for i in range(n)],
            "entry_time": pd.date_range("2026-03-01", periods=n, freq="h", tz="UTC"),
        }
    )
    out = assign_split(frame)
    assert int((out["split"] == "development").sum()) == 694
    assert int((out["split"] == "validation").sum()) == 231
    assert int((out["split"] == "test").sum()) == 233


def test_last_closed_bars_at_0946():
    from research.stoch_fade_trade_context_analysis.pipeline import expected_bar_times

    exp = expected_bar_times(pd.Timestamp("2026-08-16T09:46:00Z"))
    assert exp["1m"] == ("2026-08-16T09:45:00Z", "2026-08-16T09:46:00Z")
    assert exp["5m"] == ("2026-08-16T09:40:00Z", "2026-08-16T09:45:00Z")
    assert exp["15m"] == ("2026-08-16T09:30:00Z", "2026-08-16T09:45:00Z")
    assert exp["30m"] == ("2026-08-16T09:00:00Z", "2026-08-16T09:30:00Z")
    assert exp["1h"] == ("2026-08-16T08:00:00Z", "2026-08-16T09:00:00Z")
    assert exp["4h"] == ("2026-08-16T04:00:00Z", "2026-08-16T08:00:00Z")


def test_w3_short_mirror_and_missing():
    short_over = pre_entry_progress(direction="SHORT", entry_price=90, wave_end_price=100, tp_price=80)
    assert abs(short_over["pre_entry_progress"] - 0.5) < 1e-12
    short_neg = pre_entry_progress(direction="SHORT", entry_price=101, wave_end_price=100, tp_price=80)
    assert short_neg["pre_entry_progress"] < 0
