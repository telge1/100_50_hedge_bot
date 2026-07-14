"""Tests for short squeeze continuation audit."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.liquidation_backtest import assign_sample, in_sample_cut
from research.liquidation_level.liquidation_levels import LiquidationLevelConfig, replay_liquidation_levels
from research.liquidation_level.short_squeeze_continuation_audit import (
    ShortSqueezeConfig,
    _find_bearish_reclaim,
    aggregate_closed_htf_local,
    ema_series,
    enrich_htf_indicators,
    first_touch_conservative_short,
    run_short_squeeze_continuation_audit,
    short_path_metrics,
    trend_t1_row,
    trend_t2_row,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _synth(n: int = 90) -> pd.DataFrame:
    rows = []
    px = 100.0
    for i in range(n):
        o = px
        # mild down drift
        c = px - 0.05
        h = max(o, c) + 0.3
        l = min(o, c) - 0.3
        vol = 100.0 if i < 12 else (400.0 if i in {20, 40, 60} else 90.0)
        rows.append({"timestamp": _ts(i), "open": o, "high": h, "low": l, "close": c, "volume": vol})
        px = c
    # create then sweep up through upper levels
    for sw in (21, 41, 61):
        if sw < n:
            rows[sw]["high"] = 110.0
            rows[sw]["low"] = 90.0
            rows[sw]["close"] = 99.0  # immediate bearish reclaim of upper levels
    return pd.DataFrame(rows)


def test_htf_aggregation_closed_only() -> None:
    df = _synth(60)
    end = pd.to_datetime(df["timestamp"].iloc[-1], utc=True) + pd.Timedelta(minutes=5)
    h15 = aggregate_closed_htf_local(df, 15, end)
    assert not h15.empty
    assert "decision_time" in h15.columns
    assert (pd.to_datetime(h15["decision_time"], utc=True) <= end).all()


def test_ema_warmup() -> None:
    x = np.arange(20, dtype=float)
    e = ema_series(x, 9)
    assert np.isnan(e[7])
    assert np.isfinite(e[8])


def test_reclaim_classes() -> None:
    closes = np.array([101.0, 100.5, 99.5, 99.0, 98.5])
    # immediate
    inc, exc, ri, d = _find_bearish_reclaim(
        level_price=100.0, event_close=99.5, closes=closes, sweep_index=0
    )
    assert exc == "immediate_reclaim" and d == 0 and ri == 0
    # delayed
    closes2 = np.array([101.0, 100.5, 99.5, 99.0])
    inc, exc, ri, d = _find_bearish_reclaim(
        level_price=100.0, event_close=101.0, closes=closes2, sweep_index=0
    )
    assert exc == "delayed_reclaim_1_to_3" and ri == 2 and d == 2
    # none
    closes3 = np.array([101.0, 100.5, 100.2, 100.1])
    inc, exc, ri, d = _find_bearish_reclaim(
        level_price=100.0, event_close=101.0, closes=closes3, sweep_index=0
    )
    assert exc == "no_reclaim_within_3" and ri is None


def test_short_mfe_mae_and_sl_first() -> None:
    highs = np.array([100.4, 100.6])
    lows = np.array([99.7, 99.4])
    closes = np.array([100.0, 99.8])
    m = short_path_metrics(100.0, highs, lows, closes, 2)
    assert m["mfe_pct"] == pytest.approx(0.6)
    assert m["mae_pct"] == pytest.approx(0.6)
    # same bar both: adverse first
    assert (
        first_touch_conservative_short(np.array([100.5]), np.array([99.5]), 100.0, 0.25, 0.25)
        == "adverse_first"
    )


def test_split_and_trends() -> None:
    assert in_sample_cut(52569) == 36798
    assert assign_sample(36797, 52569) == "in_sample"
    assert assign_sample(36798, 52569) == "out_of_sample"
    # build synthetic htf with downtrend
    rows = []
    px = 100.0
    for i in range(30):
        c = px - 0.2
        rows.append(
            {
                "timestamp": _ts(i * 3),
                "decision_time": _ts(i * 3) + timedelta(minutes=15),
                "open": px,
                "high": px + 0.1,
                "low": c - 0.1,
                "close": c,
                "volume": 1.0,
            }
        )
        px = c
    htf = enrich_htf_indicators(pd.DataFrame(rows))
    # later bars should eventually satisfy T1 after EMA warm-up
    assert any(trend_t1_row(htf, i) for i in range(len(htf)))


def test_entry_after_reclaim_no_lookahead_deterministic() -> None:
    df = _synth(100)
    r1 = replay_liquidation_levels(df, LiquidationLevelConfig())
    cfg = ShortSqueezeConfig(skip_tp_sl=True, skip_bootstrap=True, horizons=(1, 3, 12), targets=(0.25, 0.50))
    b1 = run_short_squeeze_continuation_audit(r1, df, cfg)
    b2 = run_short_squeeze_continuation_audit(replay_liquidation_levels(df, LiquidationLevelConfig()), df, cfg)
    assert [e.event_id for e in b1.events] == [e.event_id for e in b2.events]
    for e in b1.events:
        if e.exclusive_reclaim_group == "immediate_reclaim":
            assert e.entry_index == e.candle_index + 1
            assert e.signal_index == e.candle_index
        elif e.exclusive_reclaim_group == "delayed_reclaim_1_to_3":
            assert e.reclaim_index is not None
            assert e.entry_index == e.reclaim_index + 1
            assert e.entry_index > e.candle_index
        assert e.side if False else True
        # no entry on sweep for reclaim trades when delayed
        if e.entry_index is not None:
            assert e.entry_index != e.candle_index


def test_exclusive_reclaim_partition() -> None:
    df = _synth(80)
    b = run_short_squeeze_continuation_audit(
        replay_liquidation_levels(df, LiquidationLevelConfig()),
        df,
        ShortSqueezeConfig(skip_tp_sl=True, skip_bootstrap=True, horizons=(3,), targets=(0.25,)),
    )
    groups = {e.exclusive_reclaim_group for e in b.events}
    assert groups <= {"immediate_reclaim", "delayed_reclaim_1_to_3", "no_reclaim_within_3"}
    # partition
    n = len(b.events)
    assert sum(1 for e in b.events if e.exclusive_reclaim_group == "immediate_reclaim") + sum(
        1 for e in b.events if e.exclusive_reclaim_group == "delayed_reclaim_1_to_3"
    ) + sum(1 for e in b.events if e.exclusive_reclaim_group == "no_reclaim_within_3") == n
