"""Tests for short-squeeze path / excursion audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_backtest import in_sample_cut
from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels
from research.liquidation_level.short_squeeze_path_audit import (
    DEFAULT_PATH_HORIZONS,
    PathAuditConfig,
    analyze_short_path,
    classify_path_category,
    run_short_squeeze_path_audit,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _path(**kwargs):
    highs = kwargs.pop("highs")
    lows = kwargs.pop("lows")
    closes = kwargs.pop("closes")
    entry_price = kwargs.get("entry_price", 100.0)
    horizon = kwargs.get("horizon", len(highs))
    ts = pd.Series([_ts(i) for i in range(len(highs))])
    return analyze_short_path(
        entry_index=0,
        entry_price=entry_price,
        highs=np.asarray(highs, float),
        lows=np.asarray(lows, float),
        closes=np.asarray(closes, float),
        timestamps=ts,
        horizon=horizon,
    )


def test_adverse_favorable_and_peak_before_trough() -> None:
    p = _path(
        highs=[101.0, 100.5, 100.2],
        lows=[99.8, 98.0, 97.5],
        closes=[100.5, 98.5, 98.0],
        horizon=3,
    )
    assert p is not None
    assert p["max_adverse_move_pct"] == pytest.approx(1.0)
    assert p["candle_of_max_adverse"] == 1
    assert p["minutes_to_max_adverse"] == 5.0
    assert p["max_favorable_move_pct"] == pytest.approx(2.5)
    assert p["candle_of_max_favorable"] == 3
    assert p["minutes_to_max_favorable"] == 15.0
    assert p["adverse_peak_before_favorable_trough"] is True
    assert p["drop_from_peak_pct"] == pytest.approx((1 - 97.5 / 101.0) * 100.0)
    assert p["candles_from_peak_to_trough"] == 2
    assert p["minutes_from_peak_to_trough"] == 10.0


def test_trough_before_peak() -> None:
    p = _path(highs=[100.2, 102.0], lows=[99.0, 99.5], closes=[99.5, 101.0], horizon=2)
    assert p is not None
    assert p["favorable_trough_before_adverse_peak"] is True
    assert p["trough_to_subsequent_peak_pct"] == pytest.approx((102.0 / 99.0 - 1.0) * 100.0)


def test_same_candle_peak_trough() -> None:
    p = _path(highs=[101.0], lows=[99.0], closes=[100.0], horizon=1)
    assert p is not None
    assert p["same_candle_peak_and_trough"] is True
    assert p["candles_between_adverse_peak_and_favorable_trough"] == 0


def test_no_future_trough_before_peak_in_drop() -> None:
    p = _path(
        highs=[100.2, 103.0, 102.0],
        lows=[97.0, 101.0, 100.0],
        closes=[99.0, 102.0, 101.0],
        horizon=3,
    )
    assert p is not None
    assert p["price_at_max_adverse"] == pytest.approx(103.0)
    assert p["subsequent_lowest_low_price"] == pytest.approx(100.0)


def test_horizons_10_20_30_40_50() -> None:
    n = 60
    highs = np.linspace(100.5, 103.0, n)
    lows = np.linspace(99.5, 97.0, n)
    closes = (highs + lows) / 2.0
    ts = pd.Series([_ts(i) for i in range(n)])
    for h in (10, 20, 30, 40, 50):
        p = analyze_short_path(
            entry_index=0,
            entry_price=100.0,
            highs=highs,
            lows=lows,
            closes=closes,
            timestamps=ts,
            horizon=h,
        )
        assert p is not None
        assert p["complete_horizon"] is True
        assert p["bars_available"] == h
        assert p["minutes_to_max_adverse"] == pytest.approx(h * 5.0)
        assert p["minutes_to_max_favorable"] == pytest.approx(h * 5.0)


def test_default_horizons_include_key_windows() -> None:
    for h in (1, 3, 5, 10, 12, 15, 20, 25, 30, 40, 50):
        assert h in DEFAULT_PATH_HORIZONS


def test_path_categories() -> None:
    def fake(**kwargs):
        base = {
            "max_adverse_move_pct": 0.0,
            "max_favorable_move_pct": 0.0,
            "drop_from_peak_pct": 0.0,
            "adverse_peak_before_favorable_trough": True,
            "same_candle_peak_and_trough": False,
        }
        base.update(kwargs)
        return base

    assert classify_path_category(fake(max_adverse_move_pct=0.1, max_favorable_move_pct=0.8)) == "immediate_drop"
    assert (
        classify_path_category(
            fake(max_adverse_move_pct=0.4, drop_from_peak_pct=0.7, adverse_peak_before_favorable_trough=True)
        )
        == "squeeze_then_drop"
    )
    assert (
        classify_path_category(
            fake(max_adverse_move_pct=1.2, drop_from_peak_pct=1.5, adverse_peak_before_favorable_trough=True)
        )
        == "deep_squeeze_then_drop"
    )
    assert (
        classify_path_category(fake(max_adverse_move_pct=0.8, drop_from_peak_pct=0.2)) == "squeeze_without_drop"
    )
    assert (
        classify_path_category(fake(max_adverse_move_pct=1.5, drop_from_peak_pct=0.2)) == "immediate_breakout"
    )
    assert classify_path_category(fake(max_adverse_move_pct=0.2, max_favorable_move_pct=0.2)) == "sideways_noise"


def test_running_mfe_mae_and_profiles() -> None:
    p = _path(
        highs=[100.5, 101.0, 100.8],
        lows=[99.8, 99.5, 98.0],
        closes=[100.2, 100.0, 98.5],
        horizon=3,
    )
    assert p is not None
    adv = np.maximum.accumulate(p["adverse_path"])
    fav = np.maximum.accumulate(p["favorable_path"])
    assert adv[-1] == pytest.approx(p["max_adverse_move_pct"])
    assert fav[-1] == pytest.approx(p["max_favorable_move_pct"])
    assert len(p["close_path"]) == 3


def test_end_of_data_incomplete_excluded_from_complete() -> None:
    highs = np.array([101.0, 101.0])
    lows = np.array([99.0, 99.0])
    closes = np.array([100.0, 100.0])
    ts = pd.Series([_ts(0), _ts(1)])
    p = analyze_short_path(
        entry_index=1,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        horizon=5,
    )
    assert p is not None
    assert p["complete_horizon"] is False
    assert p["bars_available"] == 1


def test_split_constant() -> None:
    assert in_sample_cut(52569) == 36798


def _synthetic_ohlcv(n: int = 160) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        c = px - 0.04
        rows.append(
            {
                "timestamp": _ts(i),
                "open": px,
                "high": max(px, c) + 0.4,
                "low": min(px, c) - 0.4,
                "close": c,
                "volume": 100.0 if i < 12 else (450.0 if i in {30, 60, 90, 120} else 90.0),
            }
        )
        px = c
    for sw in (31, 61, 91, 121):
        if sw < n:
            rows[sw]["high"] = 110.0
            rows[sw]["low"] = 90.0
            rows[sw]["close"] = 99.0
    return pd.DataFrame(rows)


def test_full_path_audit_deterministic_smoke() -> None:
    df = _synthetic_ohlcv()
    cfg = PathAuditConfig(horizons=(1, 10, 20, 50), skip_controls=True, seed=42)
    b1 = run_short_squeeze_path_audit(replay_liquidation_levels(df, LiquidationLevelConfig()), df, cfg)
    b2 = run_short_squeeze_path_audit(replay_liquidation_levels(df, LiquidationLevelConfig()), df, cfg)
    assert len(b1.path_events) == len(b2.path_events)
    assert b1.meta["event_counts"] == b2.meta["event_counts"]
    for e in b1.events:
        if e.entry_index is not None:
            assert e.entry_index != e.candle_index
            assert e.entry_index > e.candle_index


def test_no_lookahead_entry_after_reclaim() -> None:
    df = _synthetic_ohlcv()
    bundle = run_short_squeeze_path_audit(
        replay_liquidation_levels(df, LiquidationLevelConfig()),
        df,
        PathAuditConfig(horizons=(10, 50), skip_controls=True),
    )
    for e in bundle.events:
        if e.exclusive_reclaim_group == "immediate_reclaim":
            assert e.entry_index == e.candle_index + 1
        elif e.exclusive_reclaim_group == "delayed_reclaim_1_to_3":
            assert e.reclaim_index is not None
            assert e.entry_index == e.reclaim_index + 1
            assert e.entry_index > e.candle_index


def test_full_is_oos_and_trend_groups_present() -> None:
    df = _synthetic_ohlcv(220)
    bundle = run_short_squeeze_path_audit(
        replay_liquidation_levels(df, LiquidationLevelConfig()),
        df,
        PathAuditConfig(horizons=(10, 50), skip_controls=False, seed=7),
    )
    samples = {r["sample"] for r in bundle.path_horizon_metrics}
    assert {"full", "in_sample", "out_of_sample"} <= samples
    groups = {r["group"] for r in bundle.path_horizon_metrics}
    assert "upper_50x__reclaim_within_3__T1" in groups or "upper_25x__reclaim_within_3__T1" in groups
    assert "upper_50x__reclaim_within_3__T3" in groups or "upper_25x__reclaim_within_3__T3" in groups
    assert bundle.summary_full["sample"] == "full"
    assert bundle.summary_in_sample["sample"] == "in_sample"
    assert bundle.summary_out_of_sample["sample"] == "out_of_sample"
    assert isinstance(bundle.control_comparison, list)


def test_march_flags_on_events() -> None:
    rows = []
    start = datetime(2026, 3, 5, tzinfo=timezone.utc)
    px = 100.0
    for i in range(80):
        ts = start + timedelta(minutes=5 * i)
        c = px - 0.03
        rows.append(
            {
                "timestamp": ts,
                "open": px,
                "high": max(px, c) + 0.5,
                "low": min(px, c) - 0.5,
                "close": c,
                "volume": 400.0 if i % 20 == 15 else 80.0,
            }
        )
        px = c
        if i % 20 == 16:
            rows[-1]["high"] = 108.0
            rows[-1]["low"] = 92.0
            rows[-1]["close"] = 98.5
    warm = []
    wpx = 105.0
    for i in range(40):
        warm.append(
            {
                "timestamp": start - timedelta(minutes=5 * (40 - i)),
                "open": wpx,
                "high": wpx + 0.3,
                "low": wpx - 0.3,
                "close": wpx - 0.05,
                "volume": 120.0,
            }
        )
        wpx -= 0.05
    df = pd.concat([pd.DataFrame(warm), pd.DataFrame(rows)], ignore_index=True)
    bundle = run_short_squeeze_path_audit(
        replay_liquidation_levels(df, LiquidationLevelConfig()),
        df,
        PathAuditConfig(horizons=(5, 10), skip_controls=True),
    )
    assert "n" in bundle.summary_march
    assert "n" in bundle.summary_march_06
