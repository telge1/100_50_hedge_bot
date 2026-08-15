"""Tests for move-measurement audit (fragmentation vs calculation bugs)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.trend_direction_move_measurement_audit import (
    cluster_end_bridge_unclear,
    evaluate_signal_under_definition,
    forward_window,
    mfe_mae_from_hl,
    raw_episode_end,
    signal_definitions,
    until_opposite_end,
)


def _series(dirs, prices):
    n = len(dirs)
    ts = pd.date_range("2026-04-11", periods=n, freq="5min", tz="UTC")
    rows = []
    for i, d in enumerate(dirs):
        o, h, l, c = prices[i]
        rows.append(
            {
                "i": i,
                "open_ts": ts[i],
                "close_ts": ts[i] + pd.Timedelta(minutes=5),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "direction": d,
                "major_direction": 1 if d == "BULLISH" else (-1 if d == "BEARISH" else 0),
                "protected_structure_state": "x",
                "reason": "r",
                "structure_event": "e",
            }
        )
    return pd.DataFrame(rows)


def test_percent_math_bullish_manual():
    # entry 100, high 103 -> 3%
    mfe, mae = mfe_mae_from_hl("BULLISH", 100.0, 103.0, 99.0)
    assert abs(mfe - 3.0) < 1e-12
    assert abs(mae - 1.0) < 1e-12


def test_percent_math_bearish_manual():
    # entry 0.9, low 0.873 -> 3%
    mfe, mae = mfe_mae_from_hl("BEARISH", 0.9, 0.91, 0.873)
    assert abs(mfe - 3.0) < 1e-12
    assert abs(mae - (0.91 / 0.9 - 1.0) * 100.0) < 1e-12


def test_entry_next_open_excludes_signal_candle():
    prices = [(100, 101, 99, 100.5), (100.5, 102, 100, 101), (101.2, 103, 101, 102)]
    series = _series(["UNCLEAR", "BULLISH", "BULLISH"], prices)
    sig = {
        "signal_index": 1,
        "signal_direction": "BULLISH",
        "prev_direction": "UNCLEAR",
        "decision_time_utc": "2026-04-11T00:10:00Z",
        "signal_price_close": 101.0,
    }
    out = evaluate_signal_under_definition(series, sig, end_mode="RAW_EPISODE")
    assert out["entry_next_open"] == 101.2  # open of bar 2
    # forward window starts at bar 2, not signal bar highs
    entry, highs, lows, bars = forward_window(series, 1, 3)
    assert entry == 101.2
    assert list(highs) == [103.0]


def test_raw_ends_on_first_unclear():
    dirs = ["BULLISH", "BULLISH", "UNCLEAR", "BULLISH"]
    assert raw_episode_end(dirs, 0, "BULLISH") == 2


def test_cluster_15m_bridges_short_unclear():
    # unclear for 2 bars = 10m < 15m then resume
    dirs = ["BEARISH", "BEARISH", "UNCLEAR", "UNCLEAR", "BEARISH", "BEARISH", "BULLISH"]
    end = cluster_end_bridge_unclear(dirs, 0, "BEARISH", max_unclear_minutes=15)
    assert end == 6  # stops at opposite BULLISH


def test_cluster_30m_bridges_longer_unclear():
    # 5 unclear bars = 25m
    dirs = ["BULLISH"] + ["UNCLEAR"] * 5 + ["BULLISH", "BEARISH"]
    end15 = cluster_end_bridge_unclear(dirs, 0, "BULLISH", max_unclear_minutes=15)
    end30 = cluster_end_bridge_unclear(dirs, 0, "BULLISH", max_unclear_minutes=30)
    assert end15 == 1  # cut at unclear start (25m > 15m)
    assert end30 == 7  # bridges then hits BEARISH


def test_opposite_ends_all_clusters():
    dirs = ["BEARISH", "UNCLEAR", "BEARISH", "UNCLEAR", "BULLISH"]
    assert until_opposite_end(dirs, 0, "BEARISH") == 4
    assert cluster_end_bridge_unclear(dirs, 0, "BEARISH", max_unclear_minutes=30) == 4


def test_mfe_monotonic_horizons():
    prices = [(100, 100.5, 99.5, 100)] * 2
    # build rising highs after entry
    prices += [(100, 101, 99.8, 100.5), (100.5, 102, 100, 101), (101, 104, 100.5, 103)]
    series = _series(["UNCLEAR", "BULLISH", "BULLISH", "BULLISH", "BULLISH"], prices)
    sig = {
        "signal_index": 1,
        "signal_direction": "BULLISH",
        "prev_direction": "UNCLEAR",
        "decision_time_utc": "t",
        "signal_price_close": 100.0,
    }
    out = evaluate_signal_under_definition(series, sig, end_mode="UNTIL_OPPOSITE_CONFIRMED")
    assert out["mfe_15m_pct"] <= out["mfe_30m_pct"] + 1e-9
    assert out["mae_15m_pct"] <= out["mae_30m_pct"] + 1e-9


def test_cluster_mfe_ge_raw():
    # raw ends at unclear; cluster continues into deeper low
    prices = [
        (100, 100.2, 99.8, 100),  # 0 unclear
        (100, 100.1, 99.0, 99.5),  # 1 bearish signal candle
        (99.4, 99.5, 99.2, 99.3),  # 2 entry / still bearish
        (99.3, 99.4, 99.1, 99.2),  # 3 unclear
        (99.2, 99.3, 98.0, 98.5),  # 4 bearish resume - deeper low
        (98.5, 98.6, 98.4, 98.5),  # 5
    ]
    dirs = ["UNCLEAR", "BEARISH", "BEARISH", "UNCLEAR", "BEARISH", "BEARISH"]
    series = _series(dirs, prices)
    sig = {
        "signal_index": 1,
        "signal_direction": "BEARISH",
        "prev_direction": "UNCLEAR",
        "decision_time_utc": "t",
        "signal_price_close": 99.5,
    }
    raw = evaluate_signal_under_definition(series, sig, end_mode="RAW_EPISODE")
    cl = evaluate_signal_under_definition(series, sig, end_mode="SAME_DIRECTION_CLUSTER_15M")
    assert cl["episode_mfe_pct"] + 1e-9 >= raw["episode_mfe_pct"]


def test_no_duplicate_cluster_starts():
    dirs = ["BEARISH", "UNCLEAR", "BEARISH", "UNCLEAR", "BEARISH", "BULLISH"]
    prices = [(100, 101, 99, 100)] * 6
    series = _series(dirs, prices)
    defs = signal_definitions(series)
    assert len(defs["ALL_TRANSITIONS_TO_DIRECTION"]) == 4  # 3 bearish resumes + 1 bullish
    assert len(defs["CLUSTER_START_ONLY"]) == 2  # bearish cluster start + bullish


def test_same_direction_resume_and_major_flips():
    dirs = ["BULLISH", "UNCLEAR", "BULLISH", "UNCLEAR", "BEARISH"]
    prices = [(100, 101, 99, 100)] * 5
    series = _series(dirs, prices)
    defs = signal_definitions(series)
    # transitions: start BULLISH, resume BULLISH, flip BEARISH
    assert len(defs["ALL_TRANSITIONS_TO_DIRECTION"]) == 3
    majors = defs["MAJOR_FLIPS_ONLY"]
    assert list(majors["signal_direction"])[-1:] == ["BEARISH"] or "BEARISH" in list(majors["signal_direction"])
