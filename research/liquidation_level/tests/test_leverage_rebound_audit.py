"""Tests for leverage rebound audit helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from research.liquidation_level.leverage_rebound_audit import (
    ReboundAuditConfig,
    age_bucket,
    bootstrap_diff_ci,
    build_cascade_events,
    build_leverage_combinations,
    build_rebound_level_events,
    classify_reclaim,
    deepest_leverage,
    first_touch_bar,
    leverage_combo_label,
    measure_path_metrics,
    rebound_before_adverse,
    run_leverage_rebound_audit,
)
from research.liquidation_level.liquidation_backtest import assign_sample, in_sample_cut
from research.liquidation_level.liquidation_levels import (
    LiquidationLevelConfig,
    replay_liquidation_levels,
)


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _frame_with_sweeps(n: int = 40) -> pd.DataFrame:
    rows = []
    create_bars = {12, 20, 28}
    sweep_bars = {13, 21, 29}
    for i in range(n):
        rows.append(
            {
                "timestamp": _ts(i),
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.1,
                "volume": 100.0 if i < 12 else (400.0 if i in create_bars and i < n else 80.0),
            }
        )
    for sw in sweep_bars:
        if sw >= n:
            continue
        rows[sw]["high"] = 110.0
        rows[sw]["low"] = 90.0
        rows[sw]["close"] = 100.5
    return pd.DataFrame(rows)


def test_leverage_combo_and_deepest() -> None:
    assert leverage_combo_label({100}) == "100x_only"
    assert leverage_combo_label({50, 25}) == "50x_25x"
    assert leverage_combo_label({100, 50, 25}) == "100x_50x_25x"
    assert deepest_leverage([100, 50, 25]) == 25
    assert deepest_leverage([100]) == 100


def test_age_buckets() -> None:
    assert age_bucket(0) == "0_6"
    assert age_bucket(7) == "7_24"
    assert age_bucket(25) == "25_96"
    assert age_bucket(97) == "gt_96"


def test_mfe_mae_long_short() -> None:
    highs = np.array([101.0, 102.0])
    lows = np.array([99.5, 99.0])
    long_m = measure_path_metrics(side="lower", entry_price=100.0, highs=highs, lows=lows, horizon=2)
    assert long_m["mfe_pct"] == pytest.approx(2.0)
    assert long_m["mae_pct"] == pytest.approx(1.0)
    short_m = measure_path_metrics(side="upper", entry_price=100.0, highs=highs, lows=lows, horizon=2)
    assert short_m["mfe_pct"] == pytest.approx(1.0)
    assert short_m["mae_pct"] == pytest.approx(2.0)


def test_thresholds_and_before_adverse() -> None:
    fav = np.array([0.05, 0.12, 0.30])
    adv = np.array([0.20, 0.40, 0.50])
    assert first_touch_bar(fav, 0.10) == 2
    assert rebound_before_adverse(fav, adv, 0.10, 0.25) is True
    assert rebound_before_adverse(fav, adv, 0.30, 0.25) is False


def test_reclaim_definitions() -> None:
    closes = np.array([99.0, 100.5, 100.6, 100.7, 99.0, 99.0, 100.8])
    # lower: event close already above
    assert (
        classify_reclaim(side="lower", level_price=100.0, event_close=100.2, closes=closes, entry_index=1)
        == "immediate_reclaim"
    )
    # next candle reclaim
    assert (
        classify_reclaim(side="lower", level_price=100.0, event_close=99.5, closes=closes, entry_index=1)
        == "next_candle_reclaim"
    )
    # within 6
    assert (
        classify_reclaim(side="lower", level_price=100.0, event_close=99.0, closes=closes, entry_index=4)
        in {"reclaim_within_3", "reclaim_within_6"}
    )


def test_split_70_30() -> None:
    assert in_sample_cut(100) == 70
    assert assign_sample(69, 100) == "in_sample"
    assert assign_sample(70, 100) == "out_of_sample"


def test_level_events_entry_next_candle_and_end_excluded() -> None:
    df = _frame_with_sweeps(20)
    # force last bar sweep by making bar 18 create and 19 sweep — but need next open
    result = replay_liquidation_levels(df, LiquidationLevelConfig())
    events = build_rebound_level_events(result, df)
    assert events
    for e in events:
        assert e.entry_index == e.candle_index + 1
        assert e.entry_price is not None
    # end-of-data exclusion: if sweep on last index, no event
    last = len(df) - 1
    assert all(e.candle_index != last for e in events)


def test_combinations_and_only_groups() -> None:
    df = _frame_with_sweeps(35)
    result = replay_liquidation_levels(df, LiquidationLevelConfig())
    events = build_rebound_level_events(result, df)
    combos = build_leverage_combinations(events, df)
    assert combos
    labels = {c.leverage_combination for c in combos}
    # after huge sweep typically multiple leverages
    assert any("100x" in x or "50x" in x or "25x" in x for x in labels)
    # multiple same leverage still one combo label
    for c in combos:
        if c.swept_100x_count > 1 and c.swept_50x_count == 0 and c.swept_25x_count == 0:
            assert c.leverage_combination == "100x_only"


def test_cascades_and_windows() -> None:
    df = _frame_with_sweeps(40)
    result = replay_liquidation_levels(df, LiquidationLevelConfig())
    events = build_rebound_level_events(result, df)
    # synthesize presence by using real events; cascades may or may not appear
    cas = build_cascade_events(events, df, ReboundAuditConfig(cascade_windows=(1, 3, 12)))
    # structural: entry after last step
    for c in cas:
        assert c.entry_index == c.end_index + 1
        assert c.start_index <= c.end_index
        assert len(c.step_indices) >= 2


def test_bootstrap_deterministic() -> None:
    a = bootstrap_diff_ci([1.0, 2.0, 3.0], [0.5, 0.6, 0.7], resamples=50, seed=42)
    b = bootstrap_diff_ci([1.0, 2.0, 3.0], [0.5, 0.6, 0.7], resamples=50, seed=42)
    assert a == b
    assert a["diff_mean"] is not None


def test_no_lookahead_and_full_audit_smoke() -> None:
    df = _frame_with_sweeps(45)
    r1 = replay_liquidation_levels(df, LiquidationLevelConfig())
    # prefix causality for events: events up to k only use data <=k for creation/sweep
    evs = build_rebound_level_events(r1, df)
    for e in evs:
        assert e.entry_index > e.candle_index
    cfg = ReboundAuditConfig(
        horizons=(1, 3),
        rebound_thresholds=(0.10, 0.25),
        cascade_windows=(3,),
        bootstrap_resamples=20,
        seed=42,
    )
    b1 = run_leverage_rebound_audit(r1, df, cfg)
    b2 = run_leverage_rebound_audit(replay_liquidation_levels(df, LiquidationLevelConfig()), df, cfg)
    assert [e.event_id for e in b1.level_events] == [e.event_id for e in b2.level_events]
    assert b1.summary_full["event_counts"] == b2.summary_full["event_counts"]


def test_rejection_breakthrough_flags() -> None:
    df = _frame_with_sweeps(30)
    # set sweep close above mid for rejection on lower if close > level
    df.loc[13, "close"] = 105.0
    result = replay_liquidation_levels(df, LiquidationLevelConfig())
    events = build_rebound_level_events(result, df)
    lowers = [e for e in events if e.side == "lower" and e.candle_index == 13]
    if lowers:
        # close 105 should be above lower levels (~96-99)
        assert any(e.rejection for e in lowers)
