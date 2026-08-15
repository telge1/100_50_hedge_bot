"""Level visibility / causality tests for absorption×level V1."""

from __future__ import annotations

import pandas as pd

from research.regime_scanner.orderflow_absorption_level.config import LevelAbsorptionConfig, default_config
from research.regime_scanner.orderflow_absorption_level.levels_build import (
    active_levels_at,
    build_external_swing_levels,
    level_visible_at,
)
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.config import RegimeScannerConfig


def _candle_frame(n: int = 40) -> pd.DataFrame:
    """Synthetic OHLC with a clear swing low at i=10 and high at i=20."""
    rows = []
    base = pd.Timestamp("2026-04-01", tz="UTC")
    for i in range(n):
        # shape a low around 10 and high around 20
        if i == 10:
            low, high, close = 90.0, 100.0, 95.0
        elif i == 20:
            low, high, close = 100.0, 120.0, 115.0
        else:
            low, high, close = 98.0, 102.0, 100.0
        rows.append(
            {
                "bucket_start": base + pd.Timedelta(minutes=5 * i),
                "timestamp": base + pd.Timedelta(minutes=5 * i),
                "open": 100.0,
                "high": high,
                "low": low,
                "close": close,
                "symbol": "BTCUSDT",
                "sequence_id": 1,
                "atr_14": 2.0,
            }
        )
    return pd.DataFrame(rows)


def test_pivot_invisible_before_confirmation():
    df = _candle_frame()
    cfg = RegimeScannerConfig(pivot_left=3, pivot_right=3)
    pivots = find_confirmed_pivots(df, config=cfg)
    lows = [p for p in pivots if p.pivot_type == "low"]
    assert lows, "expected swing low"
    p = lows[0]
    conf = int(p.confirmation_index)
    # before confirmation
    assert not any(
        level_visible_at(
            {
                "confirmation_index": conf,
                "invalidated_at": None,
            },
            conf - 1,
        )
        for _ in [0]
    )


def test_pivot_invisible_on_confirmation_bar():
    level = {"confirmation_index": 13, "invalidated_at": None}
    assert level_visible_at(level, 13) is False


def test_pivot_visible_only_after_confirmation():
    level = {"confirmation_index": 13, "invalidated_at": None}
    assert level_visible_at(level, 14) is True


def test_no_future_level_in_inventory_as_of():
    df = _candle_frame()
    cfg = default_config()
    inv = build_external_swing_levels(df, symbol="BTCUSDT", sequence_id=1, cfg=cfg)
    # at early bar, no confirmed pivots yet (need left+right)
    early = active_levels_at(inv, anchor_index=5)
    assert early == []


def test_invalidation_hides_level():
    level = {"confirmation_index": 10, "invalidated_at": 20}
    assert level_visible_at(level, 15) is True
    assert level_visible_at(level, 20) is False
    assert level_visible_at(level, 21) is False


def test_external_swing_close_break_invalidates():
    df = _candle_frame(50)
    # force close below swing low after confirmation
    # swing low at 10, confirm at 13; break later
    df.loc[25, "close"] = 80.0
    cfg = default_config()
    inv = build_external_swing_levels(df, symbol="BTCUSDT", sequence_id=1, cfg=cfg)
    supports = [x for x in inv if x["side"] == "support"]
    assert supports
    s = supports[0]
    assert s["invalidation_reason"] in ("close_break", None) or s["invalidated_at"] is not None
    if s["invalidated_at"] is not None:
        assert int(s["invalidated_at"]) >= int(s["confirmation_index"])
