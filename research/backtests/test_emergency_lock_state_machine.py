"""Unit tests for Emergency-Lock Phase B state machine / ledger closes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research.backtests.emergency_lock.config import (
    EmergencyLockRecoveryConfig,
    validate_phase_b_config,
)
from research.backtests.emergency_lock.position_ledger import PositionLedger
from research.backtests.emergency_lock.signals import (
    net_long_fraction,
    tranche_qty_from_full_lock,
    unlock_reference_price,
)

PKG = Path(__file__).resolve().parent / "emergency_lock"
FORBIDDEN = (
    "research.backtests.historical_backtest",
    "research.backtests.hedge_bot_original_simulator",
    "fixed_cycle_hedge_bot.fixed_cycle_strategy",
    "research.backtests.recovery_bot",
    "research.backtests.stuck_recovery",
    "research.backtests.addon_short",
    "research.backtests.dynamic_cycle_order_scaling",
)


def test_unlock_steps_refer_to_full_lock_qty() -> None:
    assert tranche_qty_from_full_lock(
        full_lock_short_qty=100.0, unlock_step_fraction=0.10
    ) == pytest.approx(10.0)
    assert tranche_qty_from_full_lock(
        full_lock_short_qty=100.0, unlock_step_fraction=0.15
    ) == pytest.approx(15.0)


def test_partial_short_close_pnl_and_avg_unchanged() -> None:
    ledger = PositionLedger(short_qty=50.0, short_avg=100.0)
    ev = ledger.close_short(qty=10.0, fill_price=90.0, fee_rate=0.0)
    assert ev["realized_pnl_delta"] == pytest.approx(10.0 * (100.0 - 90.0))
    assert ledger.short_qty == pytest.approx(40.0)
    assert ledger.short_avg == pytest.approx(100.0)
    assert ledger.realized_short_pnl == pytest.approx(100.0)


def test_full_short_close_clears_avg() -> None:
    ledger = PositionLedger(short_qty=10.0, short_avg=50.0)
    ledger.close_short(qty=10.0, fill_price=40.0, fee_rate=0.0)
    assert ledger.short_qty == pytest.approx(0.0)
    assert ledger.short_avg == pytest.approx(0.0)


def test_closing_fee_booked_immediately() -> None:
    ledger = PositionLedger(short_qty=10.0, short_avg=100.0)
    ledger.close_short(qty=10.0, fill_price=100.0, fee_rate=0.001, fee_bucket="unlock_closing")
    assert ledger.unlock_closing_fees == pytest.approx(1.0)
    assert ledger.closing_fees == pytest.approx(1.0)
    assert ledger.total_fees == pytest.approx(1.0)


def test_net_long_fraction() -> None:
    assert net_long_fraction(
        long_qty=100.0, short_qty=50.0, full_lock_short_qty=100.0
    ) == pytest.approx(0.5)


def test_validate_steps_sum_guard() -> None:
    cfg = EmergencyLockRecoveryConfig(
        unlock_rebound_pcts=(0.03, 0.05),
        unlock_steps=(0.40, 0.20),
        maximum_net_long_fraction=0.50,
    )
    with pytest.raises(ValueError, match="maximum_net_long_fraction"):
        validate_phase_b_config(cfg)


def test_relock_vwap_and_fee() -> None:
    ledger = PositionLedger(long_qty=100.0, long_avg=10.0, short_qty=90.0, short_avg=10.0)
    ledger.open_short(qty=10.0, fill_price=8.0, fee_rate=0.001, fee_bucket="relock")
    assert ledger.short_qty == pytest.approx(100.0)
    assert ledger.short_avg == pytest.approx((90.0 * 10.0 + 10.0 * 8.0) / 100.0)
    assert ledger.relock_opening_fees == pytest.approx(abs(8.0 * 10.0) * 0.001)


def test_no_overhedge_on_manual_top_up() -> None:
    ledger = PositionLedger(long_qty=10.0, long_avg=1.0, short_qty=10.0, short_avg=1.0)
    # Attempting more short than long room should be capped by caller; ledger allows
    # open but phase B caps. Here verify equal qty stays equal via emergency top-up.
    ev = ledger.emergency_short_top_up(fill_price=0.9, fee_rate=0.0)
    assert ev["qty"] == pytest.approx(0.0)


def test_unlock_reference_causal() -> None:
    assert unlock_reference_price(post_lock_low=100.0, rebound_pct=0.03) == pytest.approx(
        103.0
    )


def test_no_forbidden_imports_phase_b_modules() -> None:
    for path in sorted(PKG.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                for forbidden in FORBIDDEN:
                    assert not name.startswith(forbidden), f"{path.name} imports {name}"
