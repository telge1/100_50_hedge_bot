"""Synthetic unit tests for LuxAlgo Liquidation Levels Python replication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_levels import (
    LiquidationLevelConfig,
    MIN_MOVE_DIVISOR,
    REFERENCE_PRICE_MODES,
    compute_min_move,
    compute_reference_price,
    compute_volatility_trigger,
    compute_volume_flags,
    level_prices,
    normalize_ohlcv_dataframe,
    replay_liquidation_levels,
    strength_from_volume_flags,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _df_from_rows(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def _flat_row(i: int, px: float = 100.0, volume: float = 100.0) -> dict:
    return {
        "timestamp": _ts(i),
        "open": px,
        "high": px,
        "low": px,
        "close": px,
        "volume": volume,
    }


# ---------------------------------------------------------------------------
# 1) reference price modes
# ---------------------------------------------------------------------------
def test_all_reference_price_modes() -> None:
    df = pd.DataFrame(
        {
            "timestamp": [_ts(0)],
            "open": [10.0],
            "high": [18.0],
            "low": [8.0],
            "close": [12.0],
            "volume": [1.0],
        }
    )
    expected = {
        "open": 10.0,
        "close": 12.0,
        "oc2": 11.0,
        "hl2": 13.0,
        "hlc3": (18.0 + 8.0 + 12.0) / 3.0,
        "ohlc4": (10.0 + 18.0 + 8.0 + 12.0) / 4.0,
        "hlcc4": (18.0 + 8.0 + 12.0 + 12.0) / 4.0,
    }
    assert set(REFERENCE_PRICE_MODES) == set(expected)
    for mode, value in expected.items():
        got = float(compute_reference_price(df, mode).iloc[0])
        assert got == pytest.approx(value)


# ---------------------------------------------------------------------------
# 2) SMA-13 warm-up
# ---------------------------------------------------------------------------
def test_sma13_warmup_disables_volume_flags() -> None:
    rows = []
    for i in range(13):
        rows.append(_flat_row(i, px=100.0, volume=100.0 + i))
    # bar 12 completes SMA; before that vb_ma is NaN => flags false
    result = replay_liquidation_levels(
        _df_from_rows(rows),
        LiquidationLevelConfig(volume_threshold=1.7, volatility_threshold=0.0),
    )
    # Force-create path uses volatility=0 which never triggers lT with equal OHLC;
    # volume flags during warm-up must not create levels.
    assert result.summary["created_level_count"] == 0

    nz0, nz1, nz2, ratio = compute_volume_flags(200.0, None, 1.7)
    assert (nz0, nz1, nz2, ratio) == (False, False, False, None)


# ---------------------------------------------------------------------------
# 3-5) nzVd thresholds: under / equal / over
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "volume,thr,expected",
    [
        (169.0, 1.7, (False, False, False)),  # under nzVd0: 100*1.7=170
        (170.0, 1.7, (False, False, False)),  # equal => not >
        (170.1, 1.7, (True, False, False)),  # over nzVd0 only
        (269.0, 1.7, (True, False, False)),  # under nzVd1: 270
        (270.0, 1.7, (True, False, False)),  # equal nzVd1
        (270.1, 1.7, (True, True, False)),  # over nzVd1
        (369.0, 1.7, (True, True, False)),  # under nzVd2: 370
        (370.0, 1.7, (True, True, False)),  # equal nzVd2
        (370.1, 1.7, (True, True, True)),  # over nzVd2
    ],
)
def test_volume_flag_thresholds(volume: float, thr: float, expected: tuple[bool, bool, bool]) -> None:
    nz0, nz1, nz2, _ = compute_volume_flags(volume, 100.0, thr)
    assert (nz0, nz1, nz2) == expected


# ---------------------------------------------------------------------------
# 6) eC true / false
# ---------------------------------------------------------------------------
def test_ec_true_and_false() -> None:
    ref = 100.0
    # tiny range inside 1/333 band
    assert compute_min_move(ref, ref * (1 + 1 / MIN_MOVE_DIVISOR) * 0.999, ref) is False
    assert compute_min_move(ref, ref, ref * (1 - 1 / MIN_MOVE_DIVISOR) * 1.001) is False
    # clear upside break of min move
    assert compute_min_move(ref, ref * (1 + 1 / MIN_MOVE_DIVISOR) + 0.01, ref) is True
    # clear downside break
    assert compute_min_move(ref, ref, ref * (1 - 1 / MIN_MOVE_DIVISOR) - 0.01) is True


# ---------------------------------------------------------------------------
# 7) lT true / false
# ---------------------------------------------------------------------------
def test_lt_true_and_false() -> None:
    # ref=open=100, low=90 => ref/(ref-low)=10 <= 10 => true
    assert (
        compute_volatility_trigger(reference=100.0, high=100.0, low=90.0, volatility_threshold=10.0)
        is True
    )
    # tiny wick: ref/(ref-low)=100 > 10 => false if high also flat
    assert (
        compute_volatility_trigger(reference=100.0, high=100.0, low=99.0, volatility_threshold=10.0)
        is False
    )
    # division by zero guard: low == ref and high == ref
    assert (
        compute_volatility_trigger(reference=100.0, high=100.0, low=100.0, volatility_threshold=10.0)
        is False
    )


# ---------------------------------------------------------------------------
# 8-10) leverage prices
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("lev", [25, 50, 100])
def test_leverage_upper_lower_prices(lev: int) -> None:
    up, lo = level_prices(100.0, lev)
    assert up == pytest.approx(100.0 * (1 + 1 / lev))
    assert lo == pytest.approx(100.0 * (1 - 1 / lev))


# ---------------------------------------------------------------------------
# Helpers for create/sweep scenarios
# ---------------------------------------------------------------------------
def _volume_create_frame(
    *,
    n_warmup: int = 13,
    spike_volume: float = 400.0,
    base_volume: float = 100.0,
    open_px: float = 100.0,
    high: float = 100.5,
    low: float = 99.5,
) -> pd.DataFrame:
    rows = []
    for i in range(n_warmup - 1):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": open_px,
                "high": open_px + 0.01,
                "low": open_px - 0.01,
                "close": open_px,
                "volume": base_volume,
            }
        )
    # final bar: volume spike + enough range for eC and levels outside candle
    i = n_warmup - 1
    rows.append(
        {
            "timestamp": _ts(i),
            "open": open_px,
            "high": high,
            "low": low,
            "close": open_px,
            "volume": spike_volume,
        }
    )
    return _df_from_rows(rows)


# ---------------------------------------------------------------------------
# 11-13) create upper/lower / no create when inside candle
# ---------------------------------------------------------------------------
def test_upper_and_lower_levels_created() -> None:
    df = _volume_create_frame(high=100.5, low=99.5, spike_volume=400.0)
    cfg = LiquidationLevelConfig(leverages=(25, 50, 100), volatility_threshold=1000.0)
    result = replay_liquidation_levels(df, cfg)
    # With open=100: 25x upper=104, lower=96 — both outside [99.5,100.5]
    # 50x: 102 / 98 — 98 < 99.5? yes; 102 > 100.5 yes
    # 100x: 101 / 99 — 101 > 100.5 yes; 99 < 99.5 yes
    assert result.summary["created_upper_count"] == 3
    assert result.summary["created_lower_count"] == 3
    assert all(lvl.level_price > 100.5 for lvl in result.all_levels if lvl.side == "upper")
    assert all(lvl.level_price < 99.5 for lvl in result.all_levels if lvl.side == "lower")


def test_no_level_when_inside_candle_range() -> None:
    # Wide candle swallows 25/50/100 levels around open=100
    df = _volume_create_frame(high=110.0, low=90.0, spike_volume=400.0)
    cfg = LiquidationLevelConfig(leverages=(25, 50, 100), volatility_threshold=1000.0)
    result = replay_liquidation_levels(df, cfg)
    assert result.summary["created_level_count"] == 0


# ---------------------------------------------------------------------------
# 14-16) strength
# ---------------------------------------------------------------------------
def test_strength_1_2_3() -> None:
    assert strength_from_volume_flags(True, False, False) == 1
    assert strength_from_volume_flags(True, True, False) == 2
    assert strength_from_volume_flags(True, True, True) == 3
    # volatility-only path still strength 1
    assert strength_from_volume_flags(False, False, False) == 1

    # SMA includes the spike bar: (12*100 + V)/13. Choose V for exact strength bands.
    df = _volume_create_frame(spike_volume=200.0)  # nzVd0 only
    r1 = replay_liquidation_levels(df, LiquidationLevelConfig(leverages=(25,), volatility_threshold=1000.0))
    assert r1.all_levels
    assert all(lvl.strength == 1 for lvl in r1.all_levels)

    df2 = _volume_create_frame(spike_volume=320.0)  # nzVd1
    r2 = replay_liquidation_levels(df2, LiquidationLevelConfig(leverages=(25,), volatility_threshold=1000.0))
    assert all(lvl.strength == 2 for lvl in r2.all_levels)

    df3 = _volume_create_frame(spike_volume=500.0)  # nzVd2
    r3 = replay_liquidation_levels(df3, LiquidationLevelConfig(leverages=(25,), volatility_threshold=1000.0))
    assert all(lvl.strength == 3 for lvl in r3.all_levels)


# ---------------------------------------------------------------------------
# 17-19) sweep strict inequalities
# ---------------------------------------------------------------------------
def test_sweep_requires_strict_cross() -> None:
    # Create one upper 25x at 104 on bar 12, then touch/sweep later.
    # Keep later volumes tiny and ranges tight so no additional creates occur.
    rows = []
    for i in range(12):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 100.0,
            }
        )
    rows.append(
        {
            "timestamp": _ts(12),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 400.0,
        }
    )
    # bar 13: high == level (104), low below — must NOT sweep
    rows.append(
        {
            "timestamp": _ts(13),
            "open": 102.0,
            "high": 104.0,
            "low": 101.0,
            "close": 103.0,
            "volume": 1.0,
        }
    )
    # bar 14: strict cross through 104
    rows.append(
        {
            "timestamp": _ts(14),
            "open": 103.5,
            "high": 105.0,
            "low": 103.0,
            "close": 104.5,
            "volume": 1.0,
        }
    )
    cfg = LiquidationLevelConfig(leverages=(25,), volatility_threshold=10.0)
    result = replay_liquidation_levels(_df_from_rows(rows), cfg)
    upper = [lvl for lvl in result.all_levels if lvl.side == "upper"]
    assert len(upper) == 1
    assert upper[0].status == "swept"
    assert upper[0].swept_index == 14
    assert upper[0].age_at_sweep == 2


def test_no_sweep_when_high_equals_level() -> None:
    rows = []
    for i in range(12):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 100.0,
            }
        )
    rows.append(
        {
            "timestamp": _ts(12),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 400.0,
        }
    )
    rows.append(
        {
            "timestamp": _ts(13),
            "open": 100.0,
            "high": 104.0,  # == 25x upper
            "low": 100.0,
            "close": 102.0,
            "volume": 1.0,
        }
    )
    result = replay_liquidation_levels(
        _df_from_rows(rows), LiquidationLevelConfig(leverages=(25,), volatility_threshold=10.0)
    )
    upper = [lvl for lvl in result.all_levels if lvl.side == "upper"][0]
    assert upper.status == "active"
    assert upper.swept_index is None


def test_no_sweep_when_low_equals_level() -> None:
    rows = []
    for i in range(12):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 100.0,
            }
        )
    rows.append(
        {
            "timestamp": _ts(12),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 400.0,
        }
    )
    rows.append(
        {
            "timestamp": _ts(13),
            "open": 100.0,
            "high": 100.0,
            "low": 96.0,  # == 25x lower
            "close": 98.0,
            "volume": 1.0,
        }
    )
    result = replay_liquidation_levels(
        _df_from_rows(rows), LiquidationLevelConfig(leverages=(25,), volatility_threshold=10.0)
    )
    lower = [lvl for lvl in result.all_levels if lvl.side == "lower"][0]
    assert lower.status == "active"
    assert lower.swept_index is None


# ---------------------------------------------------------------------------
# 20-22) multi create / multi sweep / age
# ---------------------------------------------------------------------------
def test_multiple_levels_same_candle_and_age() -> None:
    df = _volume_create_frame(spike_volume=400.0)
    result = replay_liquidation_levels(
        df, LiquidationLevelConfig(leverages=(25, 50, 100), volatility_threshold=1000.0)
    )
    assert result.summary["created_level_count"] == 6
    st = result.candle_states[-1]
    assert st.created_upper == 3 and st.created_lower == 3
    assert len(st.created_level_ids) == 6


def test_multiple_sweeps_same_candle() -> None:
    rows = []
    for i in range(12):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 100.0,
            }
        )
    rows.append(
        {
            "timestamp": _ts(12),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 400.0,
        }
    )
    # sweep all three uppers and lowers in one huge bar
    rows.append(
        {
            "timestamp": _ts(13),
            "open": 100.0,
            "high": 110.0,
            "low": 90.0,
            "close": 100.0,
            "volume": 1.0,
        }
    )
    result = replay_liquidation_levels(
        _df_from_rows(rows),
        LiquidationLevelConfig(leverages=(25, 50, 100), volatility_threshold=10.0),
    )
    assert result.summary["swept_level_count"] == 6
    st = result.candle_states[-1]
    assert st.swept_upper == 3 and st.swept_lower == 3
    for lvl in result.all_levels:
        assert lvl.age_at_sweep == 1


# ---------------------------------------------------------------------------
# 23) 500-level limit
# ---------------------------------------------------------------------------
def test_max_active_level_limit() -> None:
    rows = []
    # Many creating bars with unique non-overlapping levels that stay active.
    # Use only upper via asymmetric candles and single leverage.
    for i in range(13 + 520):
        open_px = 100.0 + i * 0.01  # slowly rising so old uppers stay above later highs? 
        # Actually rising price will sweep old uppers. Use flat price + only create lowers
        # that stay below by using tiny range and never trading down.
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.4,
                "low": 99.6,
                "close": 100.0,
                # after warm-up always spike volume
                "volume": 400.0 if i >= 12 else 100.0,
            }
        )
    cfg = LiquidationLevelConfig(
        leverages=(25, 50, 100),
        volatility_threshold=1000.0,
        max_active_levels=500,
    )
    result = replay_liquidation_levels(_df_from_rows(rows), cfg)
    assert result.summary["active_level_count_end"] <= 500
    assert result.summary["removed_by_limit_count"] > 0
    removed = [lvl for lvl in result.all_levels if lvl.status == "removed"]
    assert removed
    assert all(lvl.removal_reason == "max_active_limit" for lvl in removed)
    # oldest removed ids should be the earliest created among removed
    assert min(lvl.level_id for lvl in removed) == 1


# ---------------------------------------------------------------------------
# 24) before/after state
# ---------------------------------------------------------------------------
def test_before_after_candle_state() -> None:
    rows = []
    for i in range(12):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.2,
                "low": 99.8,
                "close": 100.0,
                "volume": 100.0,
            }
        )
    rows.append(
        {
            "timestamp": _ts(12),
            "open": 100.0,
            "high": 100.5,
            "low": 99.5,
            "close": 100.0,
            "volume": 400.0,
        }
    )
    result = replay_liquidation_levels(
        _df_from_rows(rows), LiquidationLevelConfig(leverages=(25,), volatility_threshold=1000.0)
    )
    st = result.candle_states[-1]
    assert st.active_upper_before == 0 and st.active_lower_before == 0
    assert st.created_upper == 1 and st.created_lower == 1
    assert st.active_upper_after == 1 and st.active_lower_after == 1
    assert st.swept_upper == 0 and st.swept_lower == 0
    assert set(st.active_level_ids_after) == set(st.created_level_ids)


# ---------------------------------------------------------------------------
# 25) no future candles / 26) deterministic
# ---------------------------------------------------------------------------
def test_no_future_candle_usage_and_deterministic() -> None:
    rows = []
    for i in range(40):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0 + 0.01 * np.sin(i),
                "high": 100.5 + 0.01 * np.sin(i),
                "low": 99.5 + 0.01 * np.sin(i),
                "close": 100.0,
                "volume": 100.0 if i < 12 else (400.0 if i % 5 == 0 else 120.0),
            }
        )
    df = _df_from_rows(rows)
    cfg = LiquidationLevelConfig(volatility_threshold=1000.0)

    # Prefix causality: replay[:k] active set equals full replay's after-state at k-1
    full = replay_liquidation_levels(df, cfg)
    for k in (15, 25, 40):
        prefix = replay_liquidation_levels(df.iloc[:k].copy(), cfg)
        assert [lvl.level_id for lvl in prefix.active_levels] == list(
            full.candle_states[k - 1].active_level_ids_after
        )
        assert prefix.summary["created_level_count"] == sum(
            len(st.created_level_ids) for st in full.candle_states[:k]
        )

    a = replay_liquidation_levels(df, cfg)
    b = replay_liquidation_levels(df, cfg)
    assert [(x.level_id, x.level_price, x.status, x.swept_index) for x in a.all_levels] == [
        (x.level_id, x.level_price, x.status, x.swept_index) for x in b.all_levels
    ]


def test_same_bar_created_upper_cannot_be_swept() -> None:
    df = _volume_create_frame(high=100.5, low=99.5, spike_volume=400.0)
    result = replay_liquidation_levels(
        df, LiquidationLevelConfig(leverages=(25,), volatility_threshold=1000.0)
    )
    st = result.candle_states[-1]
    assert st.created_upper == 1
    assert st.swept_upper == 0
    assert result.all_levels[0].status == "active"


def test_normalize_requires_ohlcv() -> None:
    with pytest.raises(ValueError, match="missing columns"):
        normalize_ohlcv_dataframe(pd.DataFrame({"timestamp": [_ts(0)], "open": [1.0]}))
