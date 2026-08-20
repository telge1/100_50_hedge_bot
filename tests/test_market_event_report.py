"""Unit tests for market_event_report (no ClickHouse)."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from orderbook_analyse.market_event_report.classify import (
    CLASSIFICATION_LABELS,
    classify_event,
)
from orderbook_analyse.market_event_report.metrics import (
    assert_pre_features_exclude_future,
    mfe_mae_both_sides,
    mfe_mae_for_side,
    path_window_bars,
    pre_post_price_metrics,
)


def _candles_around_event(
    event: datetime,
    *,
    pre: int = 20,
    post: int = 250,
    base: float = 100.0,
) -> pd.DataFrame:
    times = [event + timedelta(minutes=i) for i in range(-pre, post + 1)]
    # Flat then a deterministic path after event
    closes = []
    for i, t in enumerate(times):
        minutes_after = (t - event).total_seconds() / 60.0
        if minutes_after < 0:
            closes.append(base)
        elif minutes_after == 0:
            closes.append(base * 1.01)  # event up 1%
        else:
            # drift up then one dip
            closes.append(base * (1.01 + 0.001 * minutes_after - (0.02 if minutes_after == 10 else 0.0)))
    closes = np.asarray(closes, dtype=float)
    highs = closes * 1.002
    lows = closes * 0.998
    # force a clear MAE on LONG around minute 10
    idx10 = times.index(event + timedelta(minutes=10))
    lows[idx10] = base * 0.97
    highs[idx10] = base * 1.015
    return pd.DataFrame(
        {
            "open_time": times,
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": np.ones(len(times)),
        }
    )


def test_mfe_mae_long_short_formulas():
    entry = 100.0
    highs = np.array([101.0, 103.0, 102.0])
    lows = np.array([99.5, 98.0, 99.0])
    closes = np.array([100.5, 102.0, 101.0])

    long = mfe_mae_for_side(entry, highs, lows, closes, "LONG")
    assert long["mfe"] == pytest.approx(0.03)  # 103/100 - 1
    assert long["mae"] == pytest.approx(0.02)  # 1 - 98/100
    assert long["time_to_mfe_m"] == 2
    assert long["time_to_mae_m"] == 2
    assert long["ret"] == pytest.approx(0.01)

    short = mfe_mae_for_side(entry, highs, lows, closes, "SHORT")
    assert short["mfe"] == pytest.approx(0.02)  # 1 - 98/100
    assert short["mae"] == pytest.approx(0.03)  # 103/100 - 1
    assert short["time_to_mfe_m"] == 2
    assert short["time_to_mae_m"] == 2
    assert short["ret"] == pytest.approx(-0.01)

    both = mfe_mae_both_sides(entry, highs, lows, closes)
    assert both["LONG"]["mfe"] == long["mfe"]
    assert both["SHORT"]["mfe"] == short["mfe"]


def test_path_windows_1h_4h_start_after_event():
    event = datetime(2026, 8, 12, 21, 24, 0)
    candles = _candles_around_event(event)
    w60 = path_window_bars(candles, event_open_time=event, horizon_m=60)
    w240 = path_window_bars(candles, event_open_time=event, horizon_m=240)
    assert len(w60) == 60
    assert len(w240) == 240
    assert w60.iloc[0]["open_time"] == event + timedelta(minutes=1)
    assert w240.iloc[0]["open_time"] == event + timedelta(minutes=1)
    assert w60.iloc[-1]["open_time"] == event + timedelta(minutes=60)
    assert w240.iloc[-1]["open_time"] == event + timedelta(minutes=240)
    # event minute itself excluded
    assert event not in set(w60["open_time"])


def test_pre_features_exclude_future():
    event = datetime(2026, 8, 12, 21, 24, 0)
    candles = _candles_around_event(event)
    metrics = pre_post_price_metrics(candles, event_open_time=event)
    assert metrics["available"] is True
    # pre windows only use bars < event
    for w in (1, 5, 15):
        block = metrics["known_before_event"][f"{w}m"]
        assert block["n_bars"] == w
    assert_pre_features_exclude_future(
        [event - timedelta(minutes=k) for k in range(1, 16)],
        event,
    )
    with pytest.raises(AssertionError):
        assert_pre_features_exclude_future([event], event)
    with pytest.raises(AssertionError):
        assert_pre_features_exclude_future([event + timedelta(minutes=1)], event)

    # after section populated; path entry is next open
    assert metrics["after_event"]["entry_next_open"] is not None
    assert metrics["path_metrics"]["60m"]["available"] is True
    assert metrics["path_metrics"]["240m"]["available"] is True
    # causality flags
    assert metrics["causality"]["pre_features_use_open_time_lt_event"] is True
    assert metrics["causality"]["path_starts_after_event_minute"] is True


def test_classification_labels_stable_set():
    assert CLASSIFICATION_LABELS == (
        "RANGE_EXPANSION",
        "FLOW_ALIGNED_MOVE",
        "FLOW_OPPOSED_MOVE",
        "POSSIBLE_ABSORPTION",
        "POSSIBLE_RECLAIM",
        "POSSIBLE_BREAKOUT",
        "NO_CLEAR_CONFIRMATION",
    )


def test_classification_range_expansion():
    out = classify_event(
        pre_range_15m=0.002,
        path_60m_long={"mfe": 0.01, "mae": 0.01},
        path_60m_short={"mfe": 0.01, "mae": 0.01},
        future_return_60m=0.0,
        event_delta_ratio=0.0,
    )
    assert "RANGE_EXPANSION" in out["labels"]
    assert out["primary"] == "RANGE_EXPANSION"


def test_classification_flow_aligned_and_opposed():
    aligned = classify_event(
        pre_range_15m=0.001,
        path_60m_long={"mfe": 0.003, "mae": 0.001},
        path_60m_short={"mfe": 0.001, "mae": 0.003},
        future_return_60m=0.01,
        event_delta_ratio=0.4,
    )
    assert "FLOW_ALIGNED_MOVE" in aligned["labels"]
    assert aligned["primary"] == "FLOW_ALIGNED_MOVE"

    opposed = classify_event(
        pre_range_15m=0.001,
        path_60m_long={"mfe": 0.001, "mae": 0.003},
        path_60m_short={"mfe": 0.003, "mae": 0.001},
        future_return_60m=-0.01,
        event_delta_ratio=0.4,
    )
    assert "FLOW_OPPOSED_MOVE" in opposed["labels"]
    assert opposed["primary"] == "FLOW_OPPOSED_MOVE"


def test_classification_absorption_and_default():
    abs_ = classify_event(
        pre_range_15m=0.001,
        path_60m_long={"mfe": 0.001, "mae": 0.001},
        path_60m_short={"mfe": 0.001, "mae": 0.001},
        future_return_60m=0.0005,
        event_delta_ratio=0.5,
    )
    assert "POSSIBLE_ABSORPTION" in abs_["labels"]
    assert abs_["primary"] == "POSSIBLE_ABSORPTION"

    none_ = classify_event(
        pre_range_15m=0.001,
        path_60m_long={"mfe": 0.001, "mae": 0.001},
        path_60m_short={"mfe": 0.001, "mae": 0.001},
        future_return_60m=0.0,
        event_delta_ratio=0.0,
    )
    assert none_["primary"] == "NO_CLEAR_CONFIRMATION"
    assert none_["labels"] == ["NO_CLEAR_CONFIRMATION"]


def test_classification_breakout_reclaim_via_lld():
    br = classify_event(
        pre_range_15m=0.001,
        path_60m_long={"mfe": 0.01, "mae": 0.001},
        path_60m_short={"mfe": 0.001, "mae": 0.01},
        future_return_60m=0.01,
        event_delta_ratio=0.0,
        lld={"available": True, "event_interaction": "break_upper"},
    )
    assert "POSSIBLE_BREAKOUT" in br["labels"]
    assert br["primary"] == "POSSIBLE_BREAKOUT"

    rc = classify_event(
        pre_range_15m=0.001,
        path_60m_long={"mfe": 0.002, "mae": 0.002},
        path_60m_short={"mfe": 0.002, "mae": 0.002},
        future_return_60m=0.0005,
        event_delta_ratio=0.0,
        lld={"available": True, "event_interaction": "break_upper+reclaim_upper"},
    )
    assert "POSSIBLE_RECLAIM" in rc["labels"]
