#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState, StrategyIntent


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_live_short_tp_relief")),
        runtime_name="test_runtime",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        order_manager=mock.Mock(),
        cancel_open_orders_by_purpose=mock.Mock(),
    )


def _long_strategy(relief_enabled: bool = False) -> FixedCycleHedgeStrategy:
    config = FixedCycleHedgeConfig(
        bot_name="long_bot_1",
        strategy_side="long",
        symbol="APTUSDT",
        price_tick_size=0.0001,
        tp_profit_target_pct=0.25,
        tp_buffer_pct=0.125,
        order_fee_rate_pct=0.055,
        cycle_short_tp_relief_enabled=relief_enabled,
        cycle_short_tp_relief_start_cycle_index=4,
        cycle_short_tp_relief_max_distance_pct_from_long_fill=4.0,
        cycle_short_tp_relief_carry_uncovered_loss_to_exit=True,
    )
    return FixedCycleHedgeStrategy(config)


def _short_strategy(relief_enabled: bool = True) -> FixedCycleHedgeStrategy:
    config = FixedCycleHedgeConfig(
        bot_name="short_bot_1",
        strategy_side="short",
        symbol="APTUSDT",
        price_tick_size=0.0001,
        tp_profit_target_pct=0.25,
        tp_buffer_pct=0.125,
        order_fee_rate_pct=0.055,
        cycle_short_tp_relief_enabled=relief_enabled,
        cycle_short_tp_relief_start_cycle_index=4,
        cycle_short_tp_relief_max_distance_pct_from_long_fill=4.0,
        cycle_short_tp_relief_carry_uncovered_loss_to_exit=True,
    )
    return FixedCycleHedgeStrategy(config)


class LiveShortTpReliefTests(unittest.TestCase):
    def test_relief_disabled_keeps_trigger_unchanged(self) -> None:
        strategy = _long_strategy(relief_enabled=False)
        runtime = RuntimeState(strategy_state={"trade_block_id": "tb-1"})
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.8,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.82,
            short_avg=1.82,
        )
        normal_trigger = 1.70
        intent = StrategyIntent(
            side="short",
            qty=10.0,
            purpose="CYCLE_4_SHORT_REDUCE",
            order_type="Limit",
            trigger_price=normal_trigger,
        )
        intent.metadata = {
            "cycle_index": 4,
            "first_leg_fill_price": 1.8323,
            "short_entry_price": 1.82,
        }

        intents = strategy._apply_live_short_tp_relief(snapshot, runtime, [intent])
        self.assertEqual(len(intents), 1)
        self.assertAlmostEqual(intents[0].trigger_price, normal_trigger)
        self.assertNotIn("short_tp_relief_cap_applied", intents[0].metadata or {})
        self.assertNotIn("short_tp_relief_state", runtime.strategy_state)

    def test_relief_enabled_below_start_cycle_index_is_noop(self) -> None:
        strategy = _long_strategy(relief_enabled=True)
        runtime = RuntimeState(strategy_state={"trade_block_id": "tb-1"})
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.8,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.82,
            short_avg=1.82,
        )
        normal_trigger = 1.70
        intent = StrategyIntent(
            side="short",
            qty=10.0,
            purpose="CYCLE_3_SHORT_REDUCE",
            order_type="Limit",
            trigger_price=normal_trigger,
        )
        intent.metadata = {
            "cycle_index": 3,
            "first_leg_fill_price": 1.8323,
            "short_entry_price": 1.82,
        }

        intents = strategy._apply_live_short_tp_relief(snapshot, runtime, [intent])
        self.assertEqual(len(intents), 1)
        self.assertAlmostEqual(intents[0].trigger_price, normal_trigger)
        self.assertNotIn("short_tp_relief_cap_applied", intents[0].metadata or {})

    def test_relief_caps_trigger_and_registers_carry_loss_once(self) -> None:
        strategy = _long_strategy(relief_enabled=True)
        runtime = RuntimeState(strategy_state={"trade_block_id": "tb-1", "pending_cycle_loss_usdt": 0.0})
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.8,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.8323,
            short_avg=1.818,
        )
        long_fill = 1.8323
        normal_trigger = 1.70  # tiefer als Floor → Relief greift
        qty = 4.738
        intent = StrategyIntent(
            side="short",
            qty=qty,
            purpose="CYCLE_4_SHORT_REDUCE",
            order_type="Limit",
            trigger_price=normal_trigger,
        )
        intent.metadata = {
            "cycle_index": 4,
            "first_leg_fill_price": long_fill,
            "short_entry_price": 1.818,
        }

        intents = strategy._apply_live_short_tp_relief(snapshot, runtime, [intent])
        self.assertEqual(len(intents), 1)
        updated_intent = intents[0]
        meta = dict(updated_intent.metadata or {})
        self.assertTrue(meta.get("cycle_short_tp_relief_enabled"))
        self.assertTrue(meta.get("short_tp_relief_cap_applied"))
        capped_price = float(meta.get("capped_short_reduce_price") or 0.0)
        self.assertGreater(capped_price, normal_trigger)
        self.assertLess(capped_price, long_fill)

        uncovered_loss = float(meta.get("uncovered_loss") or 0.0)
        self.assertGreater(uncovered_loss, 0.0)

        relief_state = runtime.strategy_state.get("short_tp_relief_state") or {}
        carry_by_block = relief_state.get("carry_loss_by_trade_block") or {}
        self.assertIn("tb-1", carry_by_block)
        self.assertAlmostEqual(carry_by_block["tb-1"], uncovered_loss)

        # Zweiter Aufruf mit demselben Intent darf Carry-Loss nicht doppelt zählen.
        intents_second = strategy._apply_live_short_tp_relief(snapshot, runtime, [updated_intent])
        meta_second = dict(intents_second[0].metadata or {})
        relief_state_second = runtime.strategy_state.get("short_tp_relief_state") or {}
        carry_by_block_second = relief_state_second.get("carry_loss_by_trade_block") or {}
        self.assertAlmostEqual(carry_by_block_second["tb-1"], uncovered_loss)
        self.assertTrue(meta_second.get("short_tp_relief_carry_already_applied"))

    def test_effective_pending_cycle_loss_includes_carry_loss(self) -> None:
        strategy = _long_strategy(relief_enabled=True)
        runtime = RuntimeState(
            strategy_state={
                "trade_block_id": "tb-1",
                "pending_cycle_loss_usdt": 1.0,
                "short_tp_relief_state": {
                    "carry_loss_by_trade_block": {"tb-1": 2.5},
                    "cumulative_carry_loss": 2.5,
                    "applied_relief_keys_by_trade_block": {},
                    "cycle_records": [],
                },
            }
        )
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=2.0,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.8,
            short_avg=1.8,
        )
        be_price = 1.9
        projection = strategy._calculate_tp_projection(be_price, snapshot, runtime)
        self.assertAlmostEqual(
            projection.pending_cycle_loss_usdt,
            3.5,  # 1.0 base + 2.5 carry
        )
        # State-Feld selbst bleibt unverändert.
        self.assertAlmostEqual(
            float(runtime.strategy_state.get("pending_cycle_loss_usdt") or 0.0),
            1.0,
        )

    def test_invalid_inputs_skip_relief_and_do_not_register_carry(self) -> None:
        strategy = _long_strategy(relief_enabled=True)
        runtime = RuntimeState(strategy_state={"trade_block_id": "tb-1"})
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.8,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=0.0,  # invalid
            short_avg=0.0,
        )
        # long_fill_price wird 0 → Guard greift.
        intent = StrategyIntent(
            side="short",
            qty=0.0,
            purpose="CYCLE_4_SHORT_REDUCE",
            order_type="Limit",
            trigger_price=0.0,
        )
        intent.metadata = {
            "cycle_index": 4,
            "first_leg_fill_price": 0.0,
            "short_entry_price": 0.0,
        }

        intents = strategy._apply_live_short_tp_relief(snapshot, runtime, [intent])
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].trigger_price, 0.0)
        self.assertNotIn("short_tp_relief_state", runtime.strategy_state)

    def test_short_primary_strategy_side_is_not_affected(self) -> None:
        strategy = _short_strategy(relief_enabled=True)
        runtime = RuntimeState(strategy_state={"trade_block_id": "tb-1"})
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.8,
            long_qty=50.0,
            short_qty=100.0,
            long_avg=1.82,
            short_avg=1.82,
        )
        normal_trigger = 1.70
        intent = StrategyIntent(
            side="short",
            qty=10.0,
            purpose="CYCLE_4_SHORT_REDUCE",
            order_type="Limit",
            trigger_price=normal_trigger,
        )
        intent.metadata = {
            "cycle_index": 4,
            "first_leg_fill_price": 1.8323,
            "short_entry_price": 1.82,
        }

        intents = strategy._apply_live_short_tp_relief(snapshot, runtime, [intent])
        self.assertEqual(len(intents), 1)
        self.assertAlmostEqual(intents[0].trigger_price, normal_trigger)
        self.assertNotIn("short_tp_relief_state", runtime.strategy_state)


if __name__ == "__main__":
    unittest.main()

