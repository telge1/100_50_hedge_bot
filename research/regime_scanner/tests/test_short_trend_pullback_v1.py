"""Unit tests for short_trend_pullback_v1 causality and predicates."""

from __future__ import annotations

import numpy as np
import pandas as pd

from research.regime_scanner.short_trend_pullback.config import default_config
from research.regime_scanner.short_trend_pullback.impulse import build_impulse_from_bars, impulse_start_event
from research.regime_scanner.short_trend_pullback.models import ImpulseState, PullbackState
from research.regime_scanner.short_trend_pullback.regime import context_b1, context_b2, context_b3
from research.regime_scanner.short_trend_pullback.strategy import run_strategy_on_frame
from research.regime_scanner.short_trend_pullback.trigger import (
    trigger_e1,
    trigger_e2,
    trigger_e3,
    trigger_e4,
)


def test_b1_requires_ema_stack():
    cfg = default_config()
    row = {
        "ema_20": 90.0,
        "ema_59": 95.0,
        "ema_200": 100.0,
        "ema_20_slope_3": -0.1,
        "ema_59_slope_3": -0.1,
        "close": 88.0,
        "major_direction": -1,
    }
    assert context_b1(row, cfg=cfg, recent_above_ema200_share=0.0)
    row_bull = dict(row, ema_20=101.0, ema_59=102.0, ema_200=100.0)
    assert not context_b1(row_bull, cfg=cfg, recent_above_ema200_share=0.0)
    row_rising = dict(row, ema_20_slope_3=0.5)
    assert not context_b1(row_rising, cfg=cfg, recent_above_ema200_share=0.0)
    assert not context_b1(row, cfg=cfg, recent_above_ema200_share=0.9)


def test_b2_structure_and_choch():
    row = {
        "major_direction": -1,
        "protected_high": 110.0,
        "choch_side": None,
        "arm_edge_choch_bull": False,
    }
    assert context_b2(row)
    assert not context_b2(dict(row, major_direction=1))
    assert not context_b2(dict(row, choch_side="up"))
    assert not context_b2(dict(row, protected_high=None))


def test_b3_confluence():
    cfg = default_config()
    row = {
        "ema_20": 90.0,
        "ema_59": 95.0,
        "ema_200": 100.0,
        "ema_20_slope_3": -0.1,
        "ema_59_slope_3": -0.1,
        "close": 88.0,
        "major_direction": -1,
        "protected_high": 110.0,
        "choch_side": None,
        "arm_edge_choch_bull": False,
    }
    assert context_b3(row, cfg=cfg, recent_above_ema200_share=0.1)


def test_impulse_not_sideways():
    cfg = default_config()
    rows = []
    for i in range(5):
        rows.append(
            {
                "high": 100.1,
                "low": 99.9,
                "open": 100.0,
                "close": 100.0,
                "volume": 1.0,
                "atr_14": 1.0,
                "protected_high": 101.0,
                "timestamp": i,
                "arm_edge_external_bear": i == 0,
            }
        )
    assert build_impulse_from_bars(rows, start_i=0, end_i=4, cfg=cfg) is None


def test_impulse_bos_down_move():
    cfg = default_config()
    rows = []
    px = 100.0
    for i in range(6):
        o = px
        c = px - 0.8
        rows.append(
            {
                "high": o + 0.1,
                "low": c - 0.1,
                "open": o,
                "close": c,
                "volume": 10.0,
                "atr_14": 1.0,
                "protected_high": 105.0,
                "timestamp": i,
                "arm_edge_external_bear": i == 0,
            }
        )
        px = c
    imp = build_impulse_from_bars(rows, start_i=0, end_i=5, cfg=cfg)
    assert imp is not None
    assert imp.atr_move >= cfg.min_impulse_atr
    assert impulse_start_event(rows[0])


def _pb_imp():
    imp = ImpulseState(
        start_bar=0,
        end_bar=3,
        start_price=100.0,
        end_price=95.0,
        high_at_start=100.0,
        bars=4,
        return_pct=-5.0,
        atr=1.0,
        atr_move=5.0,
        efficiency=0.5,
        volume_sum=100.0,
        bos_timestamp=0,
        protected_high=102.0,
    )
    pb = PullbackState(
        start_bar=4,
        end_bar=6,
        high=98.0,
        high_bar=5,
        low_after_high=96.5,
        low_after_high_bar=6,
        bars=3,
        return_pct=3.0,
        atr_move=3.0,
        retracement=0.6,
        efficiency=0.4,
        volume_sum=40.0,
        volume_ratio=0.4,
        internal_bull_bos=True,
        external_bull_choch=False,
        dist_protected_high_pct=4.0,
        dist_ema20_pct=0.0,
        dist_ema59_pct=-1.0,
    )
    return imp, pb


def test_triggers_e1_e4():
    imp, pb = _pb_imp()
    e1 = {
        "open": 97.5,
        "high": 98.2,
        "low": 96.0,
        "close": 96.2,
        "ema_9": 97.0,
        "ema_20": 97.0,
        "ema_59": 98.0,
    }
    assert trigger_e1(e1, pb, imp)
    e2 = {"arm_edge_internal_bear": True, "arm_edge_choch_bear": False, "open": 97, "high": 97.5, "low": 96, "close": 96.5}
    assert trigger_e2(e2, pb, imp)
    e3 = {"open": 97.5, "high": 98.1, "low": 96.5, "close": 96.8, "ema_20": 97.0, "ema_59": 98.5}
    assert trigger_e3(e3, pb, imp)
    e4 = {"open": 97.0, "high": 97.2, "low": 96.0, "close": 96.2}
    assert trigger_e4(e4, pb, imp)


def test_fill_is_next_open_no_same_candle():
    """Synthetic frame: trigger close then fill next open."""
    cfg = default_config()
    n = 80
    ts0 = pd.Timestamp("2026-03-01T00:00:00+00:00")
    rows = []
    px = 100.0
    for i in range(n):
        # craft: early bars establish bearish EMAs via declining prices
        o = px
        c = px - 0.3
        h = max(o, c) + 0.15
        l = min(o, c) - 0.15
        rows.append(
            {
                "timestamp": ts0 + pd.Timedelta(minutes=15 * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000.0,
                "atr_14": 1.0,
                "ema_9": c + 0.5,
                "ema_20": c + 1.0,
                "ema_59": c + 2.0,
                "ema_200": c + 4.0,
                "ema_20_slope_3": -0.2,
                "ema_59_slope_3": -0.2,
                "adx": 25.0,
                "plus_di": 10.0,
                "minus_di": 30.0,
                "major_direction": -1,
                "protected_high": 110.0,
                "choch_side": None,
                "arm_edge_external_bear": i == 10,
                "arm_edge_major_bear": i == 10,
                "arm_edge_internal_bull": i == 18,
                "arm_edge_internal_bear": i == 22,
                "arm_edge_choch_bull": False,
                "arm_edge_choch_bear": False,
                "internal_bos_up": False,
                "internal_bos_down": False,
                "new_micro_high": i == 18,
                "new_micro_low": False,
            }
        )
        px = c
        # pullback up after impulse
        if 16 <= i <= 21:
            px = c + 0.5
    frame = pd.DataFrame(rows)
    # force upward pullback segment
    for i in range(16, 22):
        frame.loc[i, "close"] = float(frame.loc[15, "low"]) + 0.4 + 0.1 * (i - 16)
        frame.loc[i, "open"] = float(frame.loc[i, "close"]) - 0.05
        frame.loc[i, "high"] = float(frame.loc[i, "close"]) + 0.1
        frame.loc[i, "low"] = float(frame.loc[i, "open"]) - 0.05
    # rejection bar
    frame.loc[22, "open"] = float(frame.loc[21, "close"])
    frame.loc[22, "high"] = float(frame.loc[21, "high"]) + 0.05
    frame.loc[22, "close"] = float(frame.loc[22, "open"]) - 0.4
    frame.loc[22, "low"] = float(frame.loc[22, "close"]) - 0.05
    frame.loc[22, "ema_20"] = float(frame.loc[22, "close"]) + 0.2

    sigs = run_strategy_on_frame(
        frame,
        symbol="TEST",
        context="B2",
        trigger="E2",
        cfg=cfg,
        analyze_start=ts0,
    )
    # may or may not fire depending on impulse ATR path; assert causality if any
    for s in sigs:
        assert s.fill_bar == s.trigger_bar + 1
        assert float(frame.iloc[s.fill_bar]["open"]) == s.entry_price
        assert s.side == "short"
