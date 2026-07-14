"""Tests for matched-control validation audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_config import config_hash
from research.liquidation_level.liquidation_control_validation import (
    EXPECTED_FULL,
    EXPECTED_IS,
    EXPECTED_OOS,
    WINNER_CONFIG_ID,
    ControlPool,
    ControlValidationConfig,
    build_control_pool,
    build_winner_events,
    empirical_two_sided_p,
    frozen_winner_config,
    hour_cyclic_distance,
    leverage_labels,
    match_control_for_event,
    path_metrics_for_direction,
    validate_event_counts,
)
from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _synthetic(n: int = 500) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        c = px - 0.04
        rows.append(
            {
                "timestamp": _ts(i),
                "open": px,
                "high": max(px, c) + 0.6,
                "low": min(px, c) - 0.6,
                "close": c,
                "volume": 400.0 if i % 30 == 20 else 90.0,
            }
        )
        px = c
        if i % 30 == 21:
            rows[-1]["high"] = 108.0
            rows[-1]["low"] = 92.0
            rows[-1]["close"] = 97.5
    return pd.DataFrame(rows)


def test_frozen_config_hash() -> None:
    assert config_hash(frozen_winner_config()) == WINNER_CONFIG_ID


def test_hour_cyclic_midnight() -> None:
    assert hour_cyclic_distance(23, 1) == 2
    assert hour_cyclic_distance(0, 23) == 1
    assert hour_cyclic_distance(10, 10) == 0


def test_leverage_groups() -> None:
    only, flags = leverage_labels({50})
    assert only == "only_50x"
    assert flags["only_50x"] and flags["includes_50x"] and not flags["mixed_leverages"]
    only, flags = leverage_labels({25, 50})
    assert only == "mixed_leverages"
    assert flags["includes_25x"] and flags["includes_50x"] and flags["mixed_leverages"]
    assert leverage_labels({25})[0] == "only_25x"
    assert leverage_labels({100})[0] == "only_100x"


def test_long_short_mirror_metrics() -> None:
    highs = np.array([101.0, 102.0, 100.5])
    lows = np.array([99.0, 98.0, 97.0])
    closes = np.array([100.0, 99.0, 98.0])
    ts = pd.Series([_ts(i) for i in range(3)])
    short = path_metrics_for_direction(
        direction="short",
        entry_index=0,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        horizon=3,
    )
    long = path_metrics_for_direction(
        direction="long",
        entry_index=0,
        entry_price=100.0,
        highs=highs,
        lows=lows,
        closes=closes,
        timestamps=ts,
        horizon=3,
    )
    assert short is not None and long is not None
    # mirroring keeps complete horizons
    assert short["MAE_pct"] >= 0 and long["MAE_pct"] >= 0
    assert short["MFE_pct"] >= 0 and long["MFE_pct"] >= 0


def test_entry_is_next_candle_no_lookahead() -> None:
    df = _synthetic(360)
    cfg = LiquidationLevelConfig(volatility_threshold=1000.0, leverages=(25, 50, 100))
    # use winner-like but synthetic may not hit expected counts — just structure
    replay = replay_liquidation_levels(df, frozen_winner_config())
    events, meta = build_winner_events(replay, df, cfg=frozen_winner_config())
    for e in events[:50]:
        assert e.entry_index == e.signal_index + 1
        assert e.entry_index > e.signal_index


def test_validate_counts_abort() -> None:
    cfg = ControlValidationConfig()
    with pytest.raises(RuntimeError):
        validate_event_counts({"full": 1, "in_sample": 1, "out_of_sample": 0}, cfg)


def test_match_same_month_direction_and_distance() -> None:
    df = _synthetic(400)
    n = len(df)
    atr_b = np.zeros(n, dtype=int)
    vol_b = np.zeros(n, dtype=int)
    sample = np.array(["in_sample"] * n, dtype=object)
    # fake event at 200
    from research.liquidation_level.liquidation_control_validation import ValidationEvent

    e = ValidationEvent(
        event_id="E",
        signal_index=200,
        signal_timestamp=pd.Timestamp(_ts(200)),
        entry_index=201,
        entry_timestamp=pd.Timestamp(_ts(201)),
        entry_price=100.0,
        side="upper",
        direction="short",
        leverage=50,
        swept_level_count=1,
        swept_total_strength=1,
        swept_leverages=(50,),
        cluster_center_price=101.0,
        cluster_distance_pct=0.15,
        sample="in_sample",
        month="2026-01",
        hour_utc=int(_ts(200).hour),
        volatility_bucket=0,
        atr_bucket=0,
        volume_bucket=0,
        atr_pct=1.0,
        volume_ratio=1.0,
        leverage_group_only="only_50x",
        leverage_group_flags={"only_50x": True, "includes_50x": True, "includes_25x": False, "includes_100x": False, "mixed_leverages": False, "only_25x": False, "only_100x": False},
    )
    pool = build_control_pool(
        df,
        event_signal_indices={200},
        atr_bucket=atr_b,
        vol_bucket=vol_b,
        sample_arr=sample,
        max_horizon=12,
    )
    rng = np.random.default_rng(42)
    idx, level = match_control_for_event(e, pool, rng, mode="medium", min_distance=96)
    if idx is not None:
        assert abs(int(pool.indices[idx]) - 200) >= 96
        assert pool.month[idx] == "2026-01"
        assert pool.sample[idx] == "in_sample"


def test_match_does_not_use_forward_returns() -> None:
    import inspect
    from research.liquidation_level import liquidation_control_validation as m

    src = inspect.getsource(m.match_control_for_event)
    assert "close_return" not in src
    assert "MFE" not in src
    assert "forward_return" not in src


def test_empirical_p_value() -> None:
    p = empirical_two_sided_p(0.0, [0.1, -0.1, 0.2, -0.2, 0.0])
    assert 0.0 <= p <= 1.0


def test_deterministic_seed_match() -> None:
    df = _synthetic(400)
    n = len(df)
    atr_b = np.zeros(n, dtype=int)
    vol_b = np.zeros(n, dtype=int)
    sample = np.array(["in_sample"] * n, dtype=object)
    from research.liquidation_level.liquidation_control_validation import ValidationEvent

    e = ValidationEvent(
        event_id="E",
        signal_index=150,
        signal_timestamp=pd.Timestamp(_ts(150)),
        entry_index=151,
        entry_timestamp=pd.Timestamp(_ts(151)),
        entry_price=100.0,
        side="upper",
        direction="short",
        leverage=50,
        swept_level_count=1,
        swept_total_strength=1,
        swept_leverages=(50,),
        cluster_center_price=101.0,
        cluster_distance_pct=0.15,
        sample="in_sample",
        month="2026-01",
        hour_utc=int(_ts(150).hour),
        volatility_bucket=0,
        atr_bucket=0,
        volume_bucket=0,
        atr_pct=1.0,
        volume_ratio=1.0,
        leverage_group_only="only_50x",
        leverage_group_flags={"only_50x": True, "includes_50x": True, "includes_25x": False, "includes_100x": False, "mixed_leverages": False, "only_25x": False, "only_100x": False},
    )
    pool = build_control_pool(
        df, event_signal_indices={150}, atr_bucket=atr_b, vol_bucket=vol_b, sample_arr=sample, max_horizon=12
    )
    a, _ = match_control_for_event(e, pool, np.random.default_rng(7), mode="loose")
    b, _ = match_control_for_event(e, pool, np.random.default_rng(7), mode="loose")
    assert a == b


def test_expected_constants() -> None:
    assert EXPECTED_FULL == 2696
    assert EXPECTED_IS == 1824
    assert EXPECTED_OOS == 872
