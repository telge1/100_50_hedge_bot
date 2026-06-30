#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _snapshot() -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="APTUSDT",
        current_price=0.99,
        long_qty=100.0,
        short_qty=80.0,
        long_avg=0.95,
        short_avg=0.94,
    )


def _context() -> StrategyContext:
    cancel_mock = mock.Mock()
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_final_exit_coverage_gate")),
        runtime_name="test_runtime",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        cancel_open_orders_by_purpose=cancel_mock,
    )


class FinalExitCoverageGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = FixedCycleHedgeStrategy()
        self.runtime_state = RuntimeState(
            strategy_state={
                "initial_entry_confirmed": True,
                "initial_structure_built": True,
                "exit_rebuild_allowed": True,
                "current_effective_cycle": 4,
                "cycle_completed_count": 1,
                "cycle_pair_count": 1,
                "last_refill_completed_cycle_index": 0,
                "pending_cycle_loss_usdt": 1.13,
                "exit_orders_stale_after_structure_fill": True,
            }
        )
        self.runtime_state.realized_long_pnl_total = -1.13
        self.runtime_state.realized_short_pnl_total = 0.0

    def test_loss_recovery_price_component_uses_pending_loss(self) -> None:
        component = self.strategy._loss_recovery_price_component(
            _snapshot(), self.runtime_state
        )
        self.assertAlmostEqual(component, 1.13 / 20.0, places=9)

    def test_strict_gate_defers_when_pending_loss_not_covered(self) -> None:
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.05,
            long_qty=100.0,
            short_qty=80.0,
            long_avg=0.95,
            short_avg=0.94,
        )
        context = _context()
        break_even = 0.93
        stale_tp_price = 0.9954
        intents = self.strategy._build_exit_intents(
            snapshot,
            self.runtime_state,
            current_cycle=4,
            break_even_price=break_even,
            tp_price=stale_tp_price,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
        )
        self.assertEqual(intents, [])
        self.assertGreaterEqual(context.cancel_open_orders_by_purpose.call_count, 1)
        self.assertTrue(self.runtime_state.strategy_state.get("force_exit_rebuild"))


if __name__ == "__main__":
    unittest.main()
