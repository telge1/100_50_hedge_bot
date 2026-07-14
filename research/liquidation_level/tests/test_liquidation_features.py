"""Tests for causal liquidation sweep features / clusters / variants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from research.liquidation_level.liquidation_features import (
    candle_geometry,
    cluster_is_swept,
    cluster_levels_by_price_gap,
    generate_signals,
    build_candle_sweep_events,
    build_cluster_features,
    build_feature_bundle,
    build_level_sweep_events,
    weighted_center,
)
from research.liquidation_level.liquidation_levels import (
    LiquidationLevel,
    LiquidationLevelConfig,
    LiquidationReplayResult,
    STATUS_ACTIVE,
    STATUS_SWEPT,
    replay_liquidation_levels,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _lvl(
    level_id: int,
    *,
    side: str,
    price: float,
    strength: int = 1,
    created: int = 0,
    leverage: int = 25,
    status: str = STATUS_ACTIVE,
    swept_index: int | None = None,
) -> LiquidationLevel:
    return LiquidationLevel(
        level_id=level_id,
        side=side,
        leverage=leverage,
        level_price=price,
        reference_price=100.0,
        created_index=created,
        created_timestamp=pd.Timestamp(_ts(created)),
        created_open=100.0,
        created_high=100.5,
        created_low=99.5,
        created_close=100.0,
        created_volume=100.0,
        volume_sma_13=100.0,
        volume_ratio=1.0,
        strength=strength,
        created_by_volume=True,
        created_by_volatility=False,
        status=status,
        swept_index=swept_index,
        swept_timestamp=None if swept_index is None else pd.Timestamp(_ts(swept_index)),
        age_at_sweep=None if swept_index is None else swept_index - created,
        removal_reason="swept" if status == STATUS_SWEPT else None,
    )


def _volume_frame(n: int = 20) -> pd.DataFrame:
    rows = []
    for i in range(n):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.5 if i != 12 else 100.5,
                "low": 99.5 if i != 12 else 99.5,
                "close": 100.2 if i != 13 else 100.2,
                "volume": 100.0 if i < 12 else (400.0 if i == 12 else 50.0),
            }
        )
    # bar 13 huge sweep through levels
    rows[13] = {
        "timestamp": _ts(13),
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 50.0,
    }
    return pd.DataFrame(rows)


def test_close_location_and_wicks() -> None:
    geo = candle_geometry(100, 110, 90, 108)
    assert geo["close_location_value"] == pytest.approx((108 - 90) / 20)
    assert geo["upper_wick_pct"] == pytest.approx(100.0 * (110 - 108) / 20)
    assert geo["lower_wick_pct"] == pytest.approx(100.0 * (100 - 90) / 20)
    assert geo["sweep_body_pct"] == pytest.approx(100.0 * 8 / 20)


def test_weighted_center() -> None:
    assert weighted_center([10.0, 20.0], [1, 3]) == pytest.approx(17.5)


def test_cluster_gap_exact_and_over() -> None:
    # 0.10% of 100 = 0.10
    a = _lvl(1, side="lower", price=100.0)
    b = _lvl(2, side="lower", price=100.10)  # exactly 0.10%
    c = _lvl(3, side="lower", price=100.1000001 + 0.01)  # clearly > 0.10% from b? 
    # gap b-> next: use 100.20 from 100.10 = 0.0999%? 0.10/100.10*100 ≈ 0.0999 < 0.10
    d = _lvl(4, side="lower", price=100.25)  # from 100.10: 0.15/100.10*100 ≈ 0.15% > 0.10
    clusters = cluster_levels_by_price_gap([a, b, d], side="lower", candle_index=0, max_gap_pct=0.10)
    assert len(clusters) == 2
    assert [x.level_id for x in clusters[0]] == [1, 2]
    assert [x.level_id for x in clusters[1]] == [4]

    # exact boundary stays together
    clusters2 = cluster_levels_by_price_gap([a, b], side="lower", candle_index=0, max_gap_pct=0.10)
    assert len(clusters2) == 1


def test_cluster_sweep_by_count_and_strength() -> None:
    c = [_lvl(1, side="lower", price=100.0, strength=1), _lvl(2, side="lower", price=100.05, strength=1)]
    ok, reason, _ = cluster_is_swept(c, {1, 2})
    assert ok and reason == "level_count"

    c2 = [_lvl(1, side="lower", price=100.0, strength=3)]
    ok2, reason2, _ = cluster_is_swept(c2, {1})
    assert ok2 and reason2 == "total_strength"

    c3 = [_lvl(1, side="lower", price=100.0, strength=2)]
    ok3, _, _ = cluster_is_swept(c3, {1})
    assert not ok3


def test_entry_next_candle_not_on_sweep() -> None:
    df = _volume_frame(20)
    result = replay_liquidation_levels(df, LiquidationLevelConfig(volatility_threshold=10.0))
    events = build_level_sweep_events(result, df)
    assert events
    for e in events:
        assert e.entry_index == e.signal_index + 1
        assert e.entry_index != e.signal_index


def test_one_candle_event_despite_multiple_same_side_levels() -> None:
    df = _volume_frame(20)
    result = replay_liquidation_levels(
        df, LiquidationLevelConfig(leverages=(25, 50, 100), volatility_threshold=10.0)
    )
    candle_events = build_candle_sweep_events(result, df)
    # bar 13 sweeps many uppers and lowers — exactly one event per side
    at_13 = [e for e in candle_events if e.signal_index == 13]
    sides = [e.side for e in at_13]
    assert sides.count("upper") == 1
    assert sides.count("lower") == 1
    up = next(e for e in at_13 if e.side == "upper")
    assert up.swept_level_count >= 2
    assert up.swept_total_strength >= up.swept_level_count


def test_variants_l_s_f() -> None:
    # synthetic candle + cluster events via feature bundle on replay frame
    df = _volume_frame(30)
    # add a second create+sweep later for more coverage
    for i in range(20, 25):
        df.loc[i, "volume"] = 100.0
    df.loc[25, "volume"] = 400.0
    df.loc[25, "high"] = 100.5
    df.loc[25, "low"] = 99.5
    df.loc[26, "high"] = 112.0
    df.loc[26, "low"] = 88.0
    df.loc[26, "close"] = 100.0
    result = replay_liquidation_levels(df, LiquidationLevelConfig(volatility_threshold=10.0))
    bundle = build_feature_bundle(result, df)
    variants = {s.variant for s in bundle.signals}
    for v in ("L1", "S1"):
        assert v in variants
    # Filtered variants may or may not appear depending on CLV/wicks; construct directly
    from research.liquidation_level.liquidation_features import CandleSweepEvent, ClusterSweepEvent

    lower = CandleSweepEvent(
        event_id="c1",
        signal_index=10,
        signal_timestamp=pd.Timestamp(_ts(10)),
        entry_index=11,
        entry_timestamp=pd.Timestamp(_ts(11)),
        side="lower",
        swept_level_count=2,
        swept_total_strength=4,
        swept_leverages=(25, 50),
        minimum_level_price=96.0,
        maximum_level_price=98.0,
        weighted_center_price=97.0,
        oldest_level_age=5,
        median_level_age=3.0,
        strongest_level_strength=2,
        sweep_candle_open=100.0,
        sweep_candle_high=101.0,
        sweep_candle_low=99.0,
        sweep_candle_close=100.8,
        sweep_candle_volume=1.0,
        sweep_body_pct=20.0,
        upper_wick_pct=10.0,
        lower_wick_pct=40.0,
        close_location_value=0.90,
        active_upper_count_before=0,
        active_lower_count_before=2,
        active_upper_strength_before=0,
        active_lower_strength_before=3,
        swept_level_ids=(1, 2),
    )
    upper = CandleSweepEvent(
        **{**lower.__dict__, "event_id": "c2", "side": "upper", "close_location_value": 0.10, "upper_wick_pct": 50.0, "lower_wick_pct": 5.0, "sweep_body_pct": 10.0}
    )
    cl = ClusterSweepEvent(
        event_id="cl1",
        cluster_id="CL",
        signal_index=10,
        signal_timestamp=pd.Timestamp(_ts(10)),
        entry_index=11,
        entry_timestamp=pd.Timestamp(_ts(11)),
        side="lower",
        swept_level_count=2,
        swept_total_strength=4,
        level_count_in_cluster=3,
        total_strength_in_cluster=5,
        min_price=96.0,
        max_price=98.0,
        center_price=97.0,
        oldest_age=5,
        median_age=3.0,
        leverage_count=2,
        sweep_candle_open=100.0,
        sweep_candle_high=101.0,
        sweep_candle_low=99.0,
        sweep_candle_close=100.8,
        sweep_candle_volume=1.0,
        sweep_body_pct=20.0,
        upper_wick_pct=10.0,
        lower_wick_pct=40.0,
        close_location_value=0.90,
        trigger_reason="level_count",
        swept_level_ids=(1, 2),
    )
    cu = ClusterSweepEvent(**{**cl.__dict__, "event_id": "cl2", "side": "upper", "close_location_value": 0.20, "upper_wick_pct": 50.0, "lower_wick_pct": 5.0, "sweep_body_pct": 10.0})
    # F_SHORT needs lower cluster CLV<=0.25
    cl_f = ClusterSweepEvent(**{**cl.__dict__, "event_id": "cl3", "close_location_value": 0.20})
    # F_LONG needs upper cluster CLV>=0.75
    cu_f = ClusterSweepEvent(**{**cu.__dict__, "event_id": "cl4", "close_location_value": 0.80})

    sigs = generate_signals([lower, upper], [cl, cu, cl_f, cu_f])
    names = {s.variant for s in sigs}
    for v in ("L1", "L2", "L3", "L4", "L5", "L6", "L7", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "F_SHORT", "F_LONG"):
        assert v in names, v
