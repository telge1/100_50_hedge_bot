"""Unit tests for Emergency-Lock PositionLedger (synthetic candles only)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research.backtests.emergency_lock.cost_model import (
    apply_short_open_slippage,
    conservative_emergency_short_fill_price,
    fee_usdt,
)
from research.backtests.emergency_lock.position_ledger import (
    PositionLedger,
    emergency_trigger_price,
    qty_from_notional,
)

PKG = Path(__file__).resolve().parent / "emergency_lock"
FORBIDDEN_IMPORT_ROOTS = (
    "research.backtests.historical_backtest",
    "research.backtests.hedge_bot_original_simulator",
    "fixed_cycle_hedge_bot.fixed_cycle_strategy",
    "research.backtests.recovery_bot",
    "research.backtests.stuck_recovery",
    "research.backtests.addon_short",
    "research.backtests.dynamic_cycle_order_scaling",
)


def test_notional_to_qty() -> None:
    assert qty_from_notional(notional_usdt=100.0, price=2.0) == pytest.approx(50.0)
    assert qty_from_notional(notional_usdt=50.0, price=2.0) == pytest.approx(25.0)


def test_unrealized_long_pnl() -> None:
    ledger = PositionLedger(long_qty=10.0, long_avg=100.0)
    assert ledger.unrealized_long_pnl(110.0) == pytest.approx(100.0)
    assert ledger.unrealized_long_pnl(90.0) == pytest.approx(-100.0)


def test_unrealized_short_pnl() -> None:
    ledger = PositionLedger(short_qty=10.0, short_avg=100.0)
    assert ledger.unrealized_short_pnl(90.0) == pytest.approx(100.0)
    assert ledger.unrealized_short_pnl(110.0) == pytest.approx(-100.0)


def test_weighted_short_avg_on_lock() -> None:
    ledger = PositionLedger()
    ledger.open_short(qty=25.0, fill_price=100.0, fee_rate=0.0)
    ledger.long_qty = 50.0
    ledger.long_avg = 100.0
    ledger.emergency_short_top_up(fill_price=90.0, fee_rate=0.0, reference_price=90.0)
    # (25*100 + 25*90) / 50 = 95
    assert ledger.short_qty == pytest.approx(50.0)
    assert ledger.short_avg == pytest.approx(95.0)


def test_emergency_top_up_equalizes_qty() -> None:
    ledger = PositionLedger(long_qty=40.0, long_avg=10.0, short_qty=10.0, short_avg=10.0)
    ledger.emergency_short_top_up(fill_price=9.0, fee_rate=0.0)
    assert ledger.short_qty == pytest.approx(ledger.long_qty)


def test_no_short_overhedging() -> None:
    ledger = PositionLedger(long_qty=10.0, long_avg=1.0, short_qty=10.0, short_avg=1.0)
    ev = ledger.emergency_short_top_up(fill_price=0.9, fee_rate=0.0)
    assert ev["qty"] == pytest.approx(0.0)
    assert ledger.short_qty == pytest.approx(10.0)


def test_fees_booked_immediately_on_open() -> None:
    ledger = PositionLedger()
    ledger.open_long(qty=10.0, fill_price=100.0, fee_rate=0.001)
    expected = fee_usdt(fill_price=100.0, qty=10.0, fee_rate=0.001)
    assert ledger.opening_fees == pytest.approx(expected)
    assert ledger.total_fees == pytest.approx(expected)
    ledger.open_short(qty=5.0, fill_price=100.0, fee_rate=0.001)
    expected2 = expected + fee_usdt(fill_price=100.0, qty=5.0, fee_rate=0.001)
    assert ledger.total_fees == pytest.approx(expected2)


def test_short_slippage_worsens_fill() -> None:
    trigger = 90.0
    fill = conservative_emergency_short_fill_price(
        trigger_price=trigger, candle_low=80.0, slippage_bps=10.0
    )
    assert fill == pytest.approx(apply_short_open_slippage(reference_price=90.0, slippage_bps=10.0))
    assert fill < trigger
    assert fill > 80.0  # must not use advantageous low


def test_trigger_price_formula() -> None:
    assert emergency_trigger_price(long_avg=100.0, emergency_trigger_pct=0.10) == pytest.approx(90.0)


def test_basket_pnl_before_lock() -> None:
    ledger = PositionLedger()
    ledger.open_long(qty=100.0 / 100.0, fill_price=100.0, fee_rate=0.0)
    ledger.open_short(qty=50.0 / 100.0, fill_price=100.0, fee_rate=0.0)
    # mark 90: long -10, short +5 => -5
    assert ledger.basket_net_pnl(90.0) == pytest.approx(-5.0)


def test_no_forbidden_strategy_imports() -> None:
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
                for forbidden in FORBIDDEN_IMPORT_ROOTS:
                    assert not name.startswith(forbidden), f"{path.name} imports {name}"
