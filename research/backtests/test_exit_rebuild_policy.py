"""Unit tests for backtest-only exit rebuild policies."""

from __future__ import annotations

import math

import pytest

from research.backtests.exit_rebuild_policy import (
    apply_exit_rebuild_policy,
    expected_trade_pnl_at_exit,
    is_exit_covered,
    round_exit_preserving_long_coverage,
    solve_fee_adjusted_long_exit,
)


def test_non_worsening_does_not_raise_long_exit() -> None:
    decision = apply_exit_rebuild_policy(
        policy="non_worsening",
        primary_side="long",
        raw_exit=2.0037,
        active_exit=1.9825,
        long_qty=20.0,
        long_avg=1.2,
        short_qty=10.0,
        short_avg=1.0,
        realized_trade_pnl=0.0,
        fee_rate=0.00055,
        tp_profit_target_pct=0.25,
        tp_buffer_pct=0.0002,
        tick_size=0.0001,
    )
    assert decision.effective_exit == pytest.approx(1.9825)
    assert decision.prevented_increase is True


def test_non_worsening_allows_lower_exit() -> None:
    decision = apply_exit_rebuild_policy(
        policy="non_worsening",
        primary_side="long",
        raw_exit=1.90,
        active_exit=1.9825,
        long_qty=20.0,
        long_avg=1.2,
        short_qty=10.0,
        short_avg=1.0,
        realized_trade_pnl=0.0,
        fee_rate=0.00055,
        tp_profit_target_pct=0.25,
        tp_buffer_pct=0.0002,
        tick_size=0.0001,
    )
    assert decision.effective_exit == pytest.approx(1.90)


def test_coverage_gate_rejects_undercovered_old_exit() -> None:
    # Old exit far below averages → not covered; must solve a higher covered exit.
    decision = apply_exit_rebuild_policy(
        policy="non_worsening_coverage_gate",
        primary_side="long",
        raw_exit=3.0,
        active_exit=0.5,
        long_qty=20.0,
        long_avg=1.2,
        short_qty=10.0,
        short_avg=1.0,
        realized_trade_pnl=-1.0,
        fee_rate=0.00055,
        tp_profit_target_pct=0.25,
        tp_buffer_pct=0.0002,
        tick_size=0.0001,
    )
    assert decision.old_exit_covered is False
    assert decision.effective_exit > 0.5
    assert is_exit_covered(
        long_qty=20.0,
        long_avg=1.2,
        short_qty=10.0,
        short_avg=1.0,
        exit_price=decision.effective_exit,
        realized_trade_pnl=-1.0,
        fee_rate=0.00055,
        required_profit=decision.required_trade_profit or 0.0,
    )


def test_inventory_mtm_solves_net_long() -> None:
    solved = solve_fee_adjusted_long_exit(
        long_qty=20.0,
        long_avg=1.2,
        short_qty=10.0,
        short_avg=1.0,
        required_profit_usdt=0.25,
        fee_rate=0.00055,
    )
    assert solved is not None
    assert solved > 1.2


def test_inventory_mtm_qty_neutral_returns_none() -> None:
    solved = solve_fee_adjusted_long_exit(
        long_qty=10.0,
        long_avg=1.0,
        short_qty=10.0,
        short_avg=1.0,
        required_profit_usdt=0.25,
        fee_rate=0.00055,
    )
    assert solved is None


def test_net_short_solve_exists() -> None:
    solved = solve_fee_adjusted_long_exit(
        long_qty=8.0,
        long_avg=1.2,
        short_qty=10.0,
        short_avg=1.0,
        required_profit_usdt=0.25,
        fee_rate=0.00055,
    )
    # Net short → formula may yield a finite price; just ensure no crash.
    assert solved is None or math.isfinite(solved)


def test_tick_rounding_preserves_coverage() -> None:
    long_qty, long_avg, short_qty, short_avg = 20.0, 1.2, 10.0, 1.0
    realized = 0.0
    fee = 0.00055
    required = 0.30
    raw = solve_fee_adjusted_long_exit(
        long_qty=long_qty,
        long_avg=long_avg,
        short_qty=short_qty,
        short_avg=short_avg,
        required_profit_usdt=required - realized,
        fee_rate=fee,
    )
    assert raw is not None
    rounded = round_exit_preserving_long_coverage(
        raw - 0.00005,
        tick_size=0.0001,
        long_qty=long_qty,
        long_avg=long_avg,
        short_qty=short_qty,
        short_avg=short_avg,
        realized_trade_pnl=realized,
        fee_rate=fee,
        required_profit=required,
        tolerance_usdt=0.02,
    )
    assert is_exit_covered(
        long_qty=long_qty,
        long_avg=long_avg,
        short_qty=short_qty,
        short_avg=short_avg,
        exit_price=rounded,
        realized_trade_pnl=realized,
        fee_rate=fee,
        required_profit=required,
    )


def test_fees_both_legs_in_expected_pnl() -> None:
    pnl = expected_trade_pnl_at_exit(
        long_qty=10.0,
        long_avg=1.0,
        short_qty=5.0,
        short_avg=1.0,
        exit_price=1.1,
        realized_trade_pnl=0.0,
        fee_rate=0.00055,
    )
    # Manual: long +1.0, short -0.5, entry fee 0.00055*15, close fee 0.00055*1.1*15
    expected = 1.0 - 0.5 - 0.00055 * 15.0 - 0.00055 * 1.1 * 15.0
    assert pnl == pytest.approx(expected)
