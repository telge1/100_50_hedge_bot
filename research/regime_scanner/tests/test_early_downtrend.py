"""Tests for research-only early-downtrend D1–D4 detectors."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.early_downtrend import (
    default_early_downtrend_config,
    run_early_downtrend_timeline,
)


def _frame_rising_then_falling() -> pd.DataFrame:
    """Synthetic 5m bars: rise then drop below EMAs with bearish DI."""
    rows = []
    ts0 = pd.Timestamp("2026-03-06T07:00:00+00:00")
    price = 1.00
    for i in range(24):
        # rise for first 8 bars, then fall hard
        if i < 8:
            price += 0.002
        else:
            price -= 0.004
        o = price
        c = price - (0.001 if i >= 8 else -0.0005)
        rows.append(
            {
                "timestamp": ts0 + pd.Timedelta(minutes=5 * i),
                "open": o,
                "high": max(o, c) + 0.001,
                "low": min(o, c) - 0.001,
                "close": c,
                "volume": 1000.0,
                "ema_9": c + (0.002 if i < 8 else -0.001),
                "ema_20": c + (0.003 if i < 8 else 0.0005),
                "ema_9_slope_3_pct": 0.2 if i < 8 else -0.3,
                "ema_20_slope_3_pct": 0.1 if i < 8 else -0.15,
                "di_spread": 10.0 if i < 8 else -8.0,
                "adx": 25.0,
                "atr": 0.003,
                "regime_15m": "bullish_trend" if i < 10 else "transition",
            }
        )
    df = pd.DataFrame(rows)
    df["decision_time"] = df["timestamp"] + pd.Timedelta(minutes=5)
    # Force close below emas on decline
    for i in range(len(df)):
        if i >= 8:
            df.loc[i, "ema_9"] = float(df.loc[i, "close"]) + 0.002
            df.loc[i, "ema_20"] = float(df.loc[i, "close"]) + 0.003
    return df


def test_configs_disabled_and_differ() -> None:
    d1 = default_early_downtrend_config(variant="D1")
    d4 = default_early_downtrend_config(variant="D4")
    assert d1.enabled is False and d4.enabled is False
    assert d1.block_on == "early"
    assert d4.block_on == "confirmed"
    assert d1.early_min_criteria <= d4.confirmed_min_criteria


def test_no_0730_hardcoded_trading_rule_in_detector() -> None:
    import inspect
    from research.regime_scanner import early_downtrend as m

    src = inspect.getsource(m.run_early_downtrend_timeline)
    assert "07:30" not in src
    assert "timestamp >=" not in src
    # Config has no clock field
    cfg = default_early_downtrend_config(variant="D2")
    assert "0730" not in str(cfg.to_dict())
    assert cfg.enabled is False


def test_timeline_causal_and_variant_runs() -> None:
    frame = _frame_rising_then_falling()
    cfg = default_early_downtrend_config(variant="D1")
    tl = run_early_downtrend_timeline(
        frame,
        cfg,
        pivots=[],
        start="2026-03-06T07:15:00+00:00",
        end="2026-03-06T09:00:00+00:00",
    )
    assert not tl.empty
    assert tl["decision_time"].is_monotonic_increasing
    # Early part should not immediately block while still rising
    early = tl[tl["decision_time"] <= "2026-03-06T07:40:00+00:00"]
    assert (early["would_block_long"] == False).all() or early["would_block_long"].sum() == 0  # noqa: E712
    # Later decline should eventually create a non-neutral state on D1
    late = tl[tl["decision_time"] >= "2026-03-06T08:00:00+00:00"]
    assert len(late) > 0
    assert (late["state"] != "neutral").any() or (late["warning_hit"] | late["early_hit"]).any()


def test_d4_stricter_than_d1_block_timing() -> None:
    frame = _frame_rising_then_falling()
    t1 = run_early_downtrend_timeline(
        frame, default_early_downtrend_config(variant="D1"), pivots=[], start="2026-03-06T07:15:00+00:00"
    )
    t4 = run_early_downtrend_timeline(
        frame, default_early_downtrend_config(variant="D4"), pivots=[], start="2026-03-06T07:15:00+00:00"
    )
    b1 = t1.loc[t1["would_block_long"] == True, "decision_time"]  # noqa: E712
    b4 = t4.loc[t4["would_block_long"] == True, "decision_time"]  # noqa: E712
    if len(b1) and len(b4):
        assert b4.iloc[0] >= b1.iloc[0]
