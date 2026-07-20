"""Unit tests for Phase F0 forward outcomes and recovery PnL accounting."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.emergency_lock.phase_f0_outcomes import (
    _recovery_attempt_pnl,
    find_rebound_entry_bar,
    find_reclaim_close_bar,
    first_touch_race,
    forward_outcomes_from_bar,
    simulate_recovery_attempt,
)
from research.backtests.emergency_lock.phase_f0_speed import PhaseF0Config


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=5 * i)


def _c(i: int, *, h: float, l: float, c: float | None = None) -> dict:
    close = float(c if c is not None else (h + l) / 2.0)
    return {
        "timestamp": _ts(i),
        "open": close,
        "high": float(h),
        "low": float(l),
        "close": close,
        "volume": 1.0,
    }


def test_forward_mfe_mae_signs() -> None:
    candles = [
        _c(0, h=100, l=99, c=100),
        _c(1, h=101.5, l=99.5, c=101),
        _c(2, h=101, l=98.5, c=99),
    ]
    rows = forward_outcomes_from_bar(
        candles, entry_bar=0, entry_price=100.0, horizons=(2,)
    )
    assert rows[0]["horizon_complete"] is True
    assert rows[0]["mfe_pct"] == pytest.approx(0.015)
    assert rows[0]["mae_pct"] == pytest.approx(0.015)


def test_incomplete_horizon_not_zero() -> None:
    candles = [_c(0, h=100, l=99, c=100), _c(1, h=100.5, l=99.5, c=100)]
    rows = forward_outcomes_from_bar(
        candles, entry_bar=0, entry_price=100.0, horizons=(48,)
    )
    assert rows[0]["horizon_complete"] is False


def test_tp_before_stop() -> None:
    candles = [
        _c(0, h=100, l=99.5, c=100),
        _c(1, h=101.2, l=99.8, c=101),
    ]
    r = first_touch_race(
        candles, entry_bar=0, entry_price=100.0, tp_pct=0.01, stop_pct=0.005
    )
    assert r["winner"] == "tp"
    assert r["bars_to_touch"] == 1


def test_stop_before_tp() -> None:
    candles = [
        _c(0, h=100, l=99.5, c=100),
        _c(1, h=100.2, l=99.4, c=99.5),
    ]
    r = first_touch_race(
        candles, entry_bar=0, entry_price=100.0, tp_pct=0.01, stop_pct=0.005
    )
    assert r["winner"] == "stop"


def test_same_bar_collision_stop_first() -> None:
    candles = [
        _c(0, h=100, l=99.5, c=100),
        _c(1, h=101.5, l=99.0, c=100.5),
    ]
    r = first_touch_race(
        candles,
        entry_bar=0,
        entry_price=100.0,
        tp_pct=0.01,
        stop_pct=0.005,
        same_bar_policy="stop_first",
    )
    assert r["same_bar_collision"] is True
    assert r["winner"] == "stop"


def test_neither() -> None:
    candles = [_c(i, h=100.2, l=99.8, c=100) for i in range(5)]
    r = first_touch_race(
        candles, entry_bar=0, entry_price=100.0, tp_pct=0.01, stop_pct=0.005
    )
    assert r["winner"] == "neither"
    assert r["window_incomplete"] is True


def test_recovery_pnl_excludes_old_short_gain() -> None:
    pnl = _recovery_attempt_pnl(
        unlock_fill=100.0,
        relock_fill=101.0,
        unlock_qty=0.25,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert pnl["gross_directional_pnl"] == pytest.approx(0.25)
    assert pnl["net_attempt_pnl"] == pytest.approx(0.25)

    pnl_stop = _recovery_attempt_pnl(
        unlock_fill=100.0,
        relock_fill=99.5,
        unlock_qty=0.25,
        fee_rate=0.0,
        slippage_bps=0.0,
    )
    assert pnl_stop["gross_directional_pnl"] == pytest.approx(-0.125)


def test_recovery_fees_both_legs() -> None:
    pnl = _recovery_attempt_pnl(
        unlock_fill=100.0,
        relock_fill=101.0,
        unlock_qty=1.0,
        fee_rate=0.001,
        slippage_bps=0.0,
    )
    assert pnl["short_close_fee"] == pytest.approx(0.1)
    assert pnl["short_reopen_fee"] == pytest.approx(0.101)
    assert pnl["net_attempt_pnl"] == pytest.approx(1.0 - 0.1 - 0.101)


def test_simulate_attempt_tp() -> None:
    candles = [
        _c(0, h=100, l=99.5, c=100),
        _c(1, h=101.5, l=100.2, c=101.2),
    ]
    cfg = PhaseF0Config(fee_rate=0.0, slippage_bps=0.0)
    out = simulate_recovery_attempt(
        candles,
        entry_bar=0,
        entry_ref_price=100.0,
        cfg=cfg,
        unlock_qty=0.25,
        variant="R0_unfiltered",
    )
    assert out["completed"] is True
    assert out["winner"] == "tp"
    assert out["net_attempt_pnl"] == pytest.approx(0.25 * 1.0)  # qty * $1 TP move


def test_r2_waits_for_rebound() -> None:
    candles = [
        _c(0, h=100.00, l=99.80, c=99.90),
        _c(1, h=99.20, l=99.00, c=99.10),  # new low 99; high << 99.495
        _c(2, h=99.30, l=98.95, c=99.00),
        _c(3, h=99.40, l=99.00, c=99.10),
        _c(4, h=99.60, l=99.05, c=99.40),  # crosses 99*1.005
    ]
    bar, _ = find_rebound_entry_bar(candles, start_bar=0, rebound_pct=0.005)
    assert bar == 4


def test_r3_requires_close_reclaim() -> None:
    candles = [
        _c(0, h=96, l=95, c=95.5),
        _c(1, h=96.5, l=95.2, c=95.8),
        _c(2, h=96.8, l=96.1, c=96.2),
    ]
    assert find_reclaim_close_bar(candles, start_bar=0, reclaim_price=96.0) == 2
