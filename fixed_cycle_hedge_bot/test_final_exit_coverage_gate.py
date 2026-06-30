#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    FixedCycleHedgeConfig,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _context(*, symbol: str = "APTUSDT") -> StrategyContext:
    cancel_mock = mock.Mock()
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_final_exit_coverage_gate")),
        runtime_name="test_runtime",
        symbol=symbol,
        category="linear",
        min_order_value=5.0,
        cancel_open_orders_by_purpose=cancel_mock,
    )


def _exit_build_state(**overrides: object) -> dict[str, object]:
    state = {
        "initial_entry_confirmed": True,
        "initial_structure_built": True,
        "exit_rebuild_allowed": True,
        "current_effective_cycle": 1,
        "cycle_completed_count": 0,
        "cycle_pair_count": 0,
        "last_refill_completed_cycle_index": 0,
        "pending_cycle_loss_usdt": 0.0,
        "exit_orders_stale_after_structure_fill": False,
    }
    state.update(overrides)
    return state


def _apt_strategy() -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            price_tick_size=0.0001,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.125,
            order_fee_rate_pct=0.055,
        )
    )


def _apt_short_strategy() -> ShortFixedCycleHedgeStrategy:
    return ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            price_tick_size=0.0001,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.125,
            order_fee_rate_pct=0.055,
        )
    )


class ShortPrimaryFinalExitCoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = _apt_short_strategy()

    def test_short_primary_final_exit_deferred_when_undercovered(self) -> None:
        runtime_state = RuntimeState(
            strategy_state=_exit_build_state(
                pending_cycle_loss_usdt=1.13,
                exit_orders_stale_after_structure_fill=True,
            )
        )
        runtime_state.realized_short_pnl_total = -1.13
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.905,
            long_qty=109.721,
            short_qty=54.860,
            long_avg=0.9114,
            short_avg=0.9114,
        )
        context = _context()
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        intents = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=1,
            break_even_price=break_even,
            tp_price=0.9000,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
        )
        self.assertEqual(intents, [])
        self.assertGreaterEqual(context.cancel_open_orders_by_purpose.call_count, 1)
        self.assertTrue(runtime_state.strategy_state.get("force_exit_rebuild"))

    def test_short_primary_final_exit_accepts_when_min_profit_covered(self) -> None:
        runtime_state = RuntimeState(strategy_state=_exit_build_state())
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.905,
            long_qty=109.721,
            short_qty=54.860,
            long_avg=0.9114,
            short_avg=0.9114,
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        projection = self.strategy._calculate_tp_projection(
            break_even, snapshot, runtime_state
        )
        context = _context()
        intents = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=1,
            break_even_price=break_even,
            tp_price=0.9000,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
        )
        self.assertTrue(intents)
        purposes = {intent.purpose for intent in intents}
        self.assertIn("SHORT_TP_EXIT", purposes)
        self.assertIn("LONG_SL_EXIT", purposes)
        trigger_prices = [float(intent.trigger_price or 0.0) for intent in intents]
        self.assertTrue(all(price > projection.tp_price - 1e-6 for price in trigger_prices))

    def test_short_primary_flat_but_negative_is_not_closed_ok(self) -> None:
        from research.backtests.backtest_report import BacktestResult
        from research.backtests.pnl_coverage_audit import (
            apply_trade_exit_quality,
            classify_trade_exit_quality,
        )

        result = BacktestResult(
            symbol="APTUSDT",
            direction="short",
            final_status="closed",
            exit_reason="flat_no_active_orders",
            realized_pnl=-0.5,
            fill_log=[
                {
                    "timestamp": "2026-01-01T00:05:00+00:00",
                    "purpose": "CYCLE_1_SHORT_REDUCE",
                    "cycle_index": 1,
                    "closed_pnl": -1.0,
                    "side": "short",
                },
                {
                    "timestamp": "2026-01-01T00:10:00+00:00",
                    "purpose": "LONG_SL_EXIT",
                    "closed_pnl": -0.05,
                    "side": "long",
                },
                {
                    "timestamp": "2026-01-01T00:10:00+00:00",
                    "purpose": "SHORT_TP_EXIT",
                    "closed_pnl": 0.1,
                    "side": "short",
                },
            ],
        )
        quality = classify_trade_exit_quality(result)
        self.assertEqual(quality, "closed_undercovered_final_exit")
        apply_trade_exit_quality(result)
        self.assertEqual(result.final_status, "closed_undercovered_final_exit")


class FinalExitCoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = _apt_strategy()

    def test_loss_recovery_price_component_uses_pending_loss(self) -> None:
        runtime_state = RuntimeState(
            strategy_state=_exit_build_state(pending_cycle_loss_usdt=1.13)
        )
        runtime_state.realized_long_pnl_total = -1.13
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.99,
            long_qty=100.0,
            short_qty=80.0,
            long_avg=0.95,
            short_avg=0.94,
        )
        component = self.strategy._loss_recovery_price_component(snapshot, runtime_state)
        self.assertAlmostEqual(component, 1.13 / 20.0, places=9)

    def test_two_to_one_basket_exit_below_entry_not_sufficient(self) -> None:
        runtime_state = RuntimeState(strategy_state=_exit_build_state())
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.905,
            long_qty=109.721,
            short_qty=54.860,
            long_avg=0.9114,
            short_avg=0.9114,
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        projection = self.strategy._calculate_tp_projection(
            break_even, snapshot, runtime_state
        )
        economics = self.strategy._evaluate_final_exit_economics(
            long_tp_price=0.9000,
            short_sl_price=0.9000,
            snapshot=snapshot,
            runtime_state=runtime_state,
            projection=projection,
        )
        self.assertLess(economics.expected_total_net_after_exit, projection.min_profit_target_usdt)
        self.assertFalse(economics.sufficient)

    def test_two_to_one_basket_uses_projection_tp_not_stale_submission_price(self) -> None:
        runtime_state = RuntimeState(strategy_state=_exit_build_state())
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.905,
            long_qty=109.721,
            short_qty=54.860,
            long_avg=0.9114,
            short_avg=0.9114,
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        projection = self.strategy._calculate_tp_projection(
            break_even, snapshot, runtime_state
        )
        self.assertGreater(projection.tp_price, snapshot.long_avg)
        context = _context()
        intents = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=1,
            break_even_price=break_even,
            tp_price=0.9000,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
        )
        self.assertTrue(intents)
        trigger_prices = [intent.trigger_price for intent in intents]
        self.assertTrue(all(price > 0.9000 for price in trigger_prices))

    def test_gate_defers_when_pending_loss_not_covered(self) -> None:
        runtime_state = RuntimeState(
            strategy_state=_exit_build_state(
                pending_cycle_loss_usdt=1.13,
                exit_orders_stale_after_structure_fill=True,
            )
        )
        runtime_state.realized_long_pnl_total = -1.13
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.905,
            long_qty=109.721,
            short_qty=54.860,
            long_avg=0.9114,
            short_avg=0.9114,
        )
        context = _context()
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        intents = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=1,
            break_even_price=break_even,
            tp_price=0.9000,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
        )
        self.assertEqual(intents, [])
        self.assertGreaterEqual(context.cancel_open_orders_by_purpose.call_count, 1)
        self.assertTrue(runtime_state.strategy_state.get("force_exit_rebuild"))


if __name__ == "__main__":
    unittest.main()
