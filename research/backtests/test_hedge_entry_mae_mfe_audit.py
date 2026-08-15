"""Unit tests for hedge entry MAE/MFE audit (no strategy mutation)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.backtests.hedge_entry_mae_mfe_audit.core import (
    excursion_path,
    first_touch,
    hedge_2to1_pnl,
    is_success,
    mae_before_target,
)


def _bars(prices: list[tuple[str, float, float, float, float]]):
    """(iso_open, o,h,l,c)"""
    out = []
    for iso, o, h, l, c in prices:
        out.append(
            {
                "timestamp": datetime.fromisoformat(iso).replace(tzinfo=timezone.utc),
                "open": o,
                "high": h,
                "low": l,
                "close": c,
            }
        )
    return out


def test_mae_calculation():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.01, 0.99, 1.0),  # entry candle
            ("2024-01-01T00:05:00", 1.0, 1.0, 0.985, 0.99),
            ("2024-01-01T00:10:00", 0.99, 1.02, 0.98, 1.01),
        ]
    )
    r = excursion_path(candles, entry_idx=0, entry_price=1.0)
    assert abs(r["mae_pct"] - (-2.0)) < 1e-9  # min 0.98


def test_mfe_calculation():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.05, 0.95, 1.0),  # entry: ignore 1.05/0.95
            ("2024-01-01T00:05:00", 1.0, 1.03, 0.99, 1.02),
        ]
    )
    r = excursion_path(candles, entry_idx=0, entry_price=1.0)
    assert abs(r["mfe_pct"] - 3.0) < 1e-9
    assert abs(r["mae_pct"] - (-1.0)) < 1e-9


def test_entry_candle_excludes_pre_entry_range():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.10, 0.90, 1.0),  # wild range — must be ignored
            ("2024-01-01T00:05:00", 1.0, 1.01, 0.995, 1.005),
        ]
    )
    r = excursion_path(candles, entry_idx=0, entry_price=1.0)
    assert r["mae_pct"] > -1.0  # not -10%
    assert r["mfe_pct"] < 5.0  # not +10%
    assert abs(r["mae_pct"] - (-0.5)) < 1e-9
    assert abs(r["mfe_pct"] - 1.0) < 1e-9


def test_mae_before_target():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
            ("2024-01-01T00:05:00", 1.0, 1.0, 0.992, 0.993),
            ("2024-01-01T00:10:00", 0.993, 1.01, 0.991, 1.01),
        ]
    )
    r = mae_before_target(candles, entry_idx=0, entry_price=1.0, target_pct=1.0)
    assert r["target_reached"] is True
    assert abs(r["mae_before_target_pct"] - (-0.9)) < 1e-9


def test_tp_first():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
            ("2024-01-01T00:05:00", 1.0, 1.012, 0.999, 1.01),
            ("2024-01-01T00:10:00", 1.01, 1.01, 0.98, 0.99),
        ]
    )
    r = first_touch(candles, entry_idx=0, entry_price=1.0, tp_pct=1.0, sl_pct=1.0)
    assert r["first_touch"] == "TP"


def test_sl_first():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
            ("2024-01-01T00:05:00", 1.0, 1.001, 0.985, 0.99),
        ]
    )
    r = first_touch(candles, entry_idx=0, entry_price=1.0, tp_pct=1.0, sl_pct=1.0)
    assert r["first_touch"] == "SL"


def test_same_candle_ambiguous():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
            ("2024-01-01T00:05:00", 1.0, 1.02, 0.98, 1.0),
        ]
    )
    r = first_touch(candles, entry_idx=0, entry_price=1.0, tp_pct=1.0, sl_pct=1.0)
    assert r["first_touch"] == "AMBIGUOUS_SAME_CANDLE"
    assert r["same_candle_ambiguous"] is True


def test_conservative_same_candle_sl():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
            ("2024-01-01T00:05:00", 1.0, 1.02, 0.98, 1.0),
        ]
    )
    r = first_touch(candles, entry_idx=0, entry_price=1.0, tp_pct=1.0, sl_pct=1.0)
    assert r["conservative_result"] == "SL"


def test_target_not_reached():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
            ("2024-01-01T00:05:00", 1.0, 1.004, 0.995, 1.0),
        ]
    )
    r = mae_before_target(candles, entry_idx=0, entry_price=1.0, target_pct=1.0)
    assert r["target_reached"] is False
    assert r.get("status") == "NOT_REACHED"


def test_incomplete_series_end():
    candles = _bars(
        [
            ("2024-01-01T00:00:00", 1.0, 1.0, 1.0, 1.0),
        ]
    )
    r = excursion_path(candles, entry_idx=0, entry_price=1.0)
    assert r["bars_used"] == 0
    assert r["data_complete"] is False


def test_success_flag():
    assert is_success("closed_ok") is True
    assert is_success("closed_profitable_with_cycle_undercoverage") is True
    assert is_success("open") is False
    assert is_success("closed_negative_pnl") is False


def test_hedge_2to1_pnl():
    up = hedge_2to1_pnl(1.0)
    assert abs(up["net_pnl_usdt"] - 0.5) < 1e-9
    down = hedge_2to1_pnl(-1.0)
    assert abs(down["net_pnl_usdt"] - (-0.5)) < 1e-9


def test_no_strategy_import_side_effects():
    """Audit module must not patch hedge strategy modules on import."""
    import importlib
    import research.backtests.hedge_entry_mae_mfe_audit.core as core

    importlib.reload(core)
    # smoke: functions exist and hedge_2to1 unchanged
    assert abs(core.hedge_2to1_pnl(2.0)["net_pnl_usdt"] - 1.0) < 1e-9
