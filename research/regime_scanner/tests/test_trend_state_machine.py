"""Unit tests for trend state machine."""

from __future__ import annotations

import inspect

import pandas as pd

from research.regime_scanner.trend_state_machine import (
    FORBIDDEN_DIRECT,
    TrendRuntime,
    assert_no_outcomes_in_snapshot,
    default_trend_state_config,
    min_hold_for,
    run_trend_state_timeline,
    step_trend_state,
    transition_allowed,
)
def test_forbidden_direct_flips() -> None:
    assert not transition_allowed("strong_bearish", "strong_bullish")
    assert not transition_allowed("strong_bullish", "strong_bearish")
    assert not transition_allowed("early_bearish", "early_bullish")
    assert ("strong_bearish", "early_bullish") in FORBIDDEN_DIRECT
    assert transition_allowed("strong_bearish", "bearish_weakening")
    assert transition_allowed("bottoming", "early_bullish")


def test_min_hold_start_values() -> None:
    cfg = default_trend_state_config()
    assert cfg.enabled is False
    assert min_hold_for("bearish_warning", cfg) == 2
    assert min_hold_for("early_bearish", cfg) == 3
    assert min_hold_for("strong_bearish", cfg) == 4
    assert min_hold_for("bottoming", cfg) == 3


def test_no_hardcoded_march_clock_rules() -> None:
    import research.regime_scanner.trend_state_machine as m
    import research.regime_scanner.trend_structure as s

    for mod in (m, s):
        src = inspect.getsource(mod)
        assert "2026-03-06" not in src
        assert "07:30" not in src


def test_warmup_unavailable_then_neutral() -> None:
    rows = []
    ts0 = pd.Timestamp("2026-01-01T00:00:00+00:00")
    price = 1.0
    n = 230
    for i in range(n):
        price += 0.001 if i < 100 else -0.002
        o = price
        c = price
        rows.append(
            {
                "timestamp": ts0 + pd.Timedelta(minutes=5 * i),
                "open": o,
                "high": o + 0.01,
                "low": o - 0.01,
                "close": c,
                "volume": 10.0,
                "atr": 0.01,
                "ema_9": c,
                "ema_20": c,
                "ema_9_slope_3_pct": -0.1 if i >= 100 else 0.1,
                "ema_20_slope_3_pct": -0.05 if i >= 100 else 0.05,
                "di_spread": -8.0 if i >= 100 else 8.0,
                "adx": 25.0,
            }
        )
    df = pd.DataFrame(rows)
    cfg = default_trend_state_config()
    snaps, rt, _ = run_trend_state_timeline(df, cfg=cfg)
    assert snaps
    assert snaps[0].current_state == "unavailable" or snaps[0].unavailable_reason is None
    # After warmup inside full run, last snapshot should not be warmup
    assert rt.state != "unavailable" or rt.unavailable_reason != "warmup"
    assert_no_outcomes_in_snapshot(snaps[-1])


def test_min_hold_blocks_immediate_exit() -> None:
    cfg = default_trend_state_config()
    rt = TrendRuntime()
    rt.state = "strong_bearish"
    rt.age_5m_bars = 0
    rt.unavailable_reason = None
    rt.entered_at = pd.Timestamp("2026-01-01T01:00:00+00:00")
    # Force bar index past warmup
    row = {
        "timestamp": pd.Timestamp("2026-01-01T02:00:00+00:00"),
        "open": 1.0,
        "high": 1.01,
        "low": 0.99,
        "close": 1.005,
        "volume": 1.0,
        "atr": 0.01,
        "ema_9": 1.0,
        "ema_20": 0.99,
        "ema_9_slope_3_pct": 0.2,
        "ema_20_slope_3_pct": 0.1,
        "di_spread": 10.0,
        "adx": 30.0,
    }
    candles = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp("2026-01-01T00:00:00+00:00") + pd.Timedelta(minutes=5 * i),
                "open": 1.0,
                "high": 1.01,
                "low": 0.99,
                "close": 1.0,
                "volume": 1.0,
            }
            for i in range(225)
        ]
    )
    decision = pd.Timestamp("2026-01-01T02:05:00+00:00")
    rt2, snap, _ = step_trend_state(
        rt,
        candle_row=row,
        pivots_5m=[],
        decision_time=decision,
        candles_5m_as_of=candles,
        bar_index=224,
        cfg=cfg,
    )
    assert rt2.state == "strong_bearish"
    assert "min_hold" in " ".join(snap.active_reasons) or snap.current_state == "strong_bearish"


def test_config_disabled_by_default() -> None:
    assert default_trend_state_config().enabled is False
