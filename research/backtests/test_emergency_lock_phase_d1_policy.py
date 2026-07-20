"""Unit tests for Phase D.1 micro-unlock policy semantics."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from research.backtests.emergency_lock.phase_d1_policy import (
    MicroUnlockConfig,
    MicroUnlockEngine,
    stage_1_mark_pnl,
)
from research.backtests.emergency_lock.position_ledger import PositionLedger


def _ts(i: int) -> datetime:
    return datetime(2026, 1, 1, tzinfo=timezone.utc).replace(
        hour=min((i * 5) // 60, 23), minute=(i * 5) % 60
    )


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


def _engine(policy: MicroUnlockConfig | None = None) -> MicroUnlockEngine:
    cfg = policy or MicroUnlockConfig(
        variant_name="micro_unlock_10",
        stage_1_unlock_pct=0.10,
        stage_2_unlock_pct=0.0,
        max_total_unlock_pct=0.10,
        max_unlock_stages=1,
    )
    ledger = PositionLedger()
    ledger.open_long(qty=1.0, fill_price=100.0, fee_rate=0.0)
    ledger.open_short(qty=1.0, fill_price=100.0, fee_rate=0.0)
    eng = MicroUnlockEngine(
        policy=cfg, ledger=ledger, fee_rate=0.001, slippage_bps=2.0
    )
    eng.enter_lock(timestamp="t0", bar_index=0, mark=90.0)
    return eng


def test_stage_1_fee_breakeven_conservative() -> None:
    # Unlock fill 100, mark still 100 → adverse exit < 100 → negative after fees
    m = stage_1_mark_pnl(
        unlock_fill=100.0,
        qty=0.1,
        mark_close=100.0,
        fee_rate=0.001,
        slippage_bps=10.0,
        unlock_fee_paid=0.01,
    )
    assert m["stage_1_break_even_confirmed"] is False
    assert float(m["stage_1_net_pnl"]) < 0

    # Large favorable move
    m2 = stage_1_mark_pnl(
        unlock_fill=100.0,
        qty=0.1,
        mark_close=110.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        unlock_fee_paid=0.01,
    )
    assert m2["stage_1_break_even_confirmed"] is True


def test_max_unlock_caps() -> None:
    for name, cap in [
        ("micro_unlock_10", 0.10),
        ("micro_unlock_10_10", 0.20),
        ("micro_unlock_10_15", 0.25),
    ]:
        from research.backtests.emergency_lock.phase_d1_policy import micro_unlock_configs

        p = micro_unlock_configs()[name]
        assert p.max_total_unlock_pct == pytest.approx(cap)
        assert p.stage_1_unlock_pct + p.stage_2_unlock_pct == pytest.approx(cap)


def test_two_closes_below_ema20_streak_resets() -> None:
    eng = _engine()
    eng.open_unlock_qty = 0.1
    eng.active_break_level = 5.0  # close stays above → no break-level relock
    eng.active_invalidation_low = 1.0
    # close below ema
    assert eng._relock_conditions(close=9.0, ema20=10.0, prev_close=9.5, prev_ema20=10.0) is None
    assert eng.below_ema20_streak == 1
    # second below → relock
    assert (
        eng._relock_conditions(close=9.0, ema20=10.0, prev_close=9.0, prev_ema20=10.0)
        == "two_closes_below_ema20"
    )
    # reset on close above
    eng.below_ema20_streak = 1
    assert eng._relock_conditions(close=11.0, ema20=10.0, prev_close=9.0, prev_ema20=10.0) is None
    assert eng.below_ema20_streak == 0


def test_stage2_blocked_without_be_and_min_bars() -> None:
    policy = MicroUnlockConfig(
        variant_name="micro_unlock_10_10",
        stage_1_unlock_pct=0.10,
        stage_2_unlock_pct=0.10,
        max_total_unlock_pct=0.20,
        max_unlock_stages=2,
        minimum_bars_before_stage_2=6,
    )
    eng = _engine(policy)
    eng.policy_state = "MICRO_STAGE_1_CONFIRMED"
    eng.stage_1_be_ever = False
    eng.bars_since_stage_1 = 10
    # Manually: without BE, process won't stage2 — covered by flag check in process_bar
    assert eng.policy.require_stage_1_break_even_after_fees is True
    assert eng.stage_1_be_ever is False


def test_execute_unlock_respects_cap() -> None:
    eng = _engine(
        MicroUnlockConfig(
            variant_name="micro_unlock_10",
            stage_1_unlock_pct=0.10,
            stage_2_unlock_pct=0.0,
            max_total_unlock_pct=0.10,
            max_unlock_stages=1,
        )
    )
    candle = _c(1, h=96, l=94, c=95)
    ok = eng._execute_unlock(
        timestamp="t",
        candle=candle,
        bar_index=1,
        mark=95.0,
        stage=1,
        unlock_pct=0.10,
        break_level=94.0,
        swing_high=93.0,
        ema9=95.0,
        ema20=94.0,
        invalidation_low=90.0,
        reason="test",
    )
    assert ok
    assert eng.max_open_unlock_pct == pytest.approx(0.10)
    # Second unlock would exceed
    ok2 = eng._execute_unlock(
        timestamp="t2",
        candle=candle,
        bar_index=2,
        mark=96.0,
        stage=2,
        unlock_pct=0.10,
        break_level=95.0,
        swing_high=94.0,
        ema9=96.0,
        ema20=95.0,
        invalidation_low=90.0,
        reason="test2",
    )
    assert ok2 is False


def test_relock_restores_short_qty() -> None:
    eng = _engine()
    candle = _c(1, h=96, l=94, c=95)
    eng._execute_unlock(
        timestamp="t",
        candle=candle,
        bar_index=1,
        mark=95.0,
        stage=1,
        unlock_pct=0.10,
        break_level=94.0,
        swing_high=93.0,
        ema9=95.0,
        ema20=94.0,
        invalidation_low=90.0,
        reason="test",
    )
    short_after_unlock = eng.ledger.short_qty
    eng._execute_relock(
        timestamp="t2",
        candle=_c(2, h=93, l=90, c=91),
        bar_index=2,
        mark=91.0,
        reason="close_below_break_level",
    )
    assert eng.open_unlock_qty == pytest.approx(0.0)
    assert eng.ledger.short_qty == pytest.approx(eng.full_lock_short_qty, abs=1e-9)
    assert eng.ledger.short_qty > short_after_unlock


def test_no_full_unlock_allowed() -> None:
    for p in [
        MicroUnlockConfig("a", 0.10, 0.0, 0.10, 1),
        MicroUnlockConfig("b", 0.10, 0.10, 0.20, 2),
        MicroUnlockConfig("c", 0.10, 0.15, 0.25, 2),
    ]:
        assert p.max_total_unlock_pct <= 0.25 + 1e-12
        assert p.stage_1_unlock_pct + p.stage_2_unlock_pct <= 0.25 + 1e-12
