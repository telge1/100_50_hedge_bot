"""Unit tests for liquidity_sweep_reclaim_v1 (no MySQL required)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.regime_scanner.liquidity_sweep_reclaim.config import (
    all_variants,
    penetration_min_atr,
    variant_id,
)
from research.regime_scanner.liquidity_sweep_reclaim.levels import (
    L3_AVAILABLE,
    eligible_levels_at_prior_bar,
    level_still_valid,
)
from research.regime_scanner.liquidity_sweep_reclaim.models import LevelSnapshot
from research.regime_scanner.liquidity_sweep_reclaim.outcomes import (
    exit_benchmark_outcome,
    forward_outcomes,
    side_sign,
)
from research.regime_scanner.liquidity_sweep_reclaim.reclaim import (
    deeper_break_before_reclaim,
    r1_same_candle,
    r2_one_bar,
    r3_confirmation_ok,
    reclaim_close,
)
from research.regime_scanner.liquidity_sweep_reclaim.sequential import apply_sequential
from research.regime_scanner.liquidity_sweep_reclaim.strategy import run_strategy_on_frame
from research.regime_scanner.liquidity_sweep_reclaim.sweep import measure_sweep, qualifies_penetration


def _ohlcv(n: int = 40, start: float = 100.0) -> pd.DataFrame:
    rows = []
    px = start
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    for i in range(n):
        o = px
        c = px + (0.1 if i % 2 == 0 else -0.1)
        h = max(o, c) + 0.5
        l = min(o, c) - 0.5
        rows.append(
            {
                "timestamp": t0 + pd.Timedelta(minutes=15 * i),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
                "volume": 1000.0 + i,
                "atr_14": 1.0,
                "atr": 1.0,
                "ema_20": px,
                "ema_59": px,
                "ema_200": px,
                "adx": 20.0,
                "major_direction": 0,
                "protected_high": 101.0,
                "protected_low": 99.0,
                "external_bos_up": False,
                "external_bos_down": False,
                "choch_side": None,
                "c31_in_range": True,
                "c31_range_high": 101.0,
                "c31_range_low": 99.0,
                "c31_range_age": 10,
                "c31_range_width_atr": 2.0,
                "c31_range_score": 0.7,
                "c31_box_efficiency": 0.8,
                "c31_bound_drift": 0.1,
                "c31_failed_breakout_event": False,
                "c31_state": "range_sideways",
            }
        )
        px = c
    return pd.DataFrame(rows)


def test_l3_unavailable_and_variant_count():
    assert L3_AVAILABLE is False
    vs = all_variants()
    assert len(vs) == 18
    assert all(not v.startswith("L3") for v in vs)
    assert variant_id("L1", "P2", "R1") == "L1xP2xR1"


def test_penetration_classes_and_oversized():
    sw = measure_sweep(
        side="long", level=100.0, high=100.5, low=99.5, open_=100.2, close=100.1, atr=1.0
    )
    assert sw is not None
    assert abs(sw["penetration_atr"] - 0.5) < 1e-9
    assert qualifies_penetration("P1", 0.05)
    assert not qualifies_penetration("P2", 0.05)
    assert qualifies_penetration("P2", 0.10)
    assert qualifies_penetration("P3", 0.25)
    assert not qualifies_penetration("P3", 0.20)
    big = measure_sweep(
        side="long", level=100.0, high=100.0, low=98.5, open_=99.8, close=99.9, atr=1.0
    )
    assert big is not None
    assert big["oversized_break"] is True
    assert not qualifies_penetration("P1", big["penetration_atr"])


def test_no_sweep_without_beyond():
    assert (
        measure_sweep(
            side="long", level=100.0, high=100.5, low=100.0, open_=100.2, close=100.1, atr=1.0
        )
        is None
    )
    assert (
        measure_sweep(
            side="short", level=100.0, high=100.0, low=99.0, open_=99.8, close=99.7, atr=1.0
        )
        is None
    )


def test_reclaim_r1_r2_r3():
    assert r1_same_candle(side="long", level=100.0, swept=True, close=100.1)
    assert not r1_same_candle(side="long", level=100.0, swept=True, close=99.9)
    assert r2_one_bar(side="short", level=100.0, bars_since_sweep=1, close=99.5)
    assert not r2_one_bar(side="short", level=100.0, bars_since_sweep=2, close=99.5)
    assert reclaim_close(side="long", level=100.0, close=100.0)  # exact
    assert r3_confirmation_ok(
        side="long", level=100.0, reclaim_close_px=100.2, open_=100.1, close=100.3
    )
    assert not r3_confirmation_ok(
        side="long", level=100.0, reclaim_close_px=100.2, open_=100.3, close=99.5
    )


def test_deeper_break():
    assert deeper_break_before_reclaim(
        side="long", level=100.0, prior_extreme=99.5, high=100.0, low=99.2
    )
    assert deeper_break_before_reclaim(
        side="short", level=100.0, prior_extreme=100.5, high=100.8, low=100.0
    )


def test_levels_prior_bar_only():
    frame = _ohlcv(20)
    # mutate current bar bounds — must not be used at i
    frame.loc[10, "c31_range_low"] = 90.0
    frame.loc[10, "protected_low"] = 90.0
    levels = eligible_levels_at_prior_bar(frame, 10, level_families=("L1", "L2"))
    longs = [x for x in levels if x.side == "long"]
    assert longs
    assert all(abs(x.level_value - 99.0) < 1e-9 for x in longs)


def test_level_invalidation_protected_bos():
    frame = _ohlcv(10)
    snap = LevelSnapshot(
        level_family="L2",
        level_id="x",
        level_value=99.0,
        side="long",
        confirmed_timestamp="t",
        confirmed_bar=5,
        age_bars=2,
    )
    frame.loc[7, "external_bos_down"] = True
    frame.loc[7, "close"] = 98.0
    ok, reason = level_still_valid(frame, 7, snap)
    assert ok is False
    assert reason == "external_bos_breakout"


def test_strategy_r1_long_fill_next_open():
    frame = _ohlcv(30)
    # bar 10: sweep below 99 and reclaim close above
    frame.loc[10, "low"] = 98.7
    frame.loc[10, "high"] = 99.5
    frame.loc[10, "open"] = 99.2
    frame.loc[10, "close"] = 99.3  # reclaim
    frame.loc[11, "open"] = 99.35
    sigs, setups = run_strategy_on_frame(
        frame,
        symbol="TEST",
        level_families=("L1",),
        penetrations=("P1",),
        reclaims=("R1",),
        analyze_start=frame["timestamp"].iloc[5],
    )
    longs = [s for s in sigs if s.side == "long" and s.reclaim_type == "R1"]
    assert longs
    s0 = longs[0]
    assert s0.fill_bar == s0.trigger_bar + 1
    assert abs(s0.entry_price - float(frame.iloc[s0.fill_bar]["open"])) < 1e-12
    # never use fill high/low/close for trigger
    assert s0.trigger_bar < s0.fill_bar


def test_strategy_r2_and_duplicate_guard():
    frame = _ohlcv(30)
    # sweep closes below level (no R1), reclaim next bar
    frame.loc[10, "low"] = 98.7
    frame.loc[10, "close"] = 98.8
    frame.loc[10, "open"] = 99.1
    frame.loc[10, "high"] = 99.2
    frame.loc[11, "close"] = 99.2
    frame.loc[11, "open"] = 98.9
    frame.loc[11, "high"] = 99.3
    frame.loc[11, "low"] = 98.85
    frame.loc[12, "open"] = 99.25
    sigs, _ = run_strategy_on_frame(
        frame,
        symbol="TEST",
        level_families=("L1",),
        penetrations=("P1",),
        reclaims=("R2",),
        analyze_start=frame["timestamp"].iloc[5],
    )
    longs = [s for s in sigs if s.side == "long"]
    assert len(longs) >= 1
    # duplicate guard: one signal per variant×side×level×sweep_bar
    keys = {(s.variant, s.side, s.sweep_timestamp) for s in longs}
    assert len(keys) == len(longs)


def test_oversized_no_signal():
    frame = _ohlcv(20)
    frame.loc[10, "low"] = 97.5  # 1.5 ATR beyond 99
    frame.loc[10, "close"] = 99.2
    frame.loc[10, "open"] = 99.0
    sigs, setups = run_strategy_on_frame(
        frame,
        symbol="TEST",
        level_families=("L1",),
        penetrations=("P1",),
        reclaims=("R1",),
        analyze_start=frame["timestamp"].iloc[5],
    )
    assert not any(s.side == "long" and s.trigger_bar == 10 for s in sigs)
    assert any(s.get("invalidation_reason") == "oversized_break" for s in setups)


def test_forward_and_exits_long_short():
    frame = _ohlcv(50)
    # force a path
    for i in range(20, 40):
        frame.loc[i, "high"] = 100 + (i - 20) * 0.1
        frame.loc[i, "low"] = 99.5
        frame.loc[i, "close"] = 100 + (i - 20) * 0.05
        frame.loc[i, "open"] = frame.loc[i, "close"] - 0.01
    fwd = forward_outcomes(frame, fill_i=20, entry=100.0, side="long")
    assert "h8_mfe_pct" in fwd
    oc = exit_benchmark_outcome(frame, fill_i=20, entry=100.0, side="long", exit_id="X1")
    assert "net_pnl_pct" in oc
    assert side_sign("short") == -1
    oc_s = exit_benchmark_outcome(frame, fill_i=20, entry=100.0, side="short", exit_id="X5")
    assert "exit_reason" in oc_s


def test_sequential_blocks_same_variant_side_coin():
    rows = []
    t0 = pd.Timestamp("2024-01-01", tz="UTC")
    for i in range(5):
        rows.append(
            {
                "variant": "L1xP1xR1",
                "symbol": "AAA",
                "side": "long",
                "exit_id": "X1",
                "fill_timestamp": t0 + pd.Timedelta(minutes=15 * i),
                "bars_held": 10,
                "net_pnl_pct": 0.1,
            }
        )
    # also other coin should not block
    rows.append(
        {
            "variant": "L1xP1xR1",
            "symbol": "BBB",
            "side": "long",
            "exit_id": "X1",
            "fill_timestamp": t0,
            "bars_held": 10,
            "net_pnl_pct": 0.1,
        }
    )
    df = apply_sequential(pd.DataFrame(rows))
    aaa = df[df["symbol"] == "AAA"]
    assert aaa.iloc[0]["taken_sequential"] is True or aaa.iloc[0]["taken_sequential"] == True  # noqa: E712
    assert aaa["taken_sequential"].sum() == 1
    assert bool(df[df["symbol"] == "BBB"].iloc[0]["taken_sequential"])


def test_penetration_min_helper():
    assert penetration_min_atr("P1") == 0.0
    assert penetration_min_atr("P2") == 0.10
    assert penetration_min_atr("P3") == 0.25
