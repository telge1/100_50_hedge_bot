#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _jtousdt_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.1"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.1"),
        "tick_size": Decimal("0.0001"),
    }


def _runtime_with_rules(*, strategy_state: dict | None = None) -> RuntimeState:
    state = strategy_state or {}
    runtime = RuntimeState(strategy_state=state)
    runtime.instrument_rules["JTOUSDT"] = _jtousdt_rules()
    return runtime


def _snapshot(*, current_price: float = 0.71) -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="JTOUSDT",
        current_price=current_price,
        long_qty=67.2,
        short_qty=84.8,
        long_avg=0.744,
        short_avg=0.7438,
    )


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_normal_second_leg_split")),
        runtime_name="test_runtime",
        symbol="JTOUSDT",
        category="linear",
        min_order_value=5.0,
    )


def _long_bot_strategy() -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="long_bot_1",
            strategy_side="long",
            symbol="JTOUSDT",
            restart=False,
            qty_step=0.1,
            min_order_qty=0.1,
            min_notional_usdt=5.0,
            price_tick_size=0.0001,
        )
    )


def _short_bot_strategy() -> ShortFixedCycleHedgeStrategy:
    return ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="short_bot_1",
            strategy_side="short",
            symbol="JTOUSDT",
            restart=False,
            qty_step=0.1,
            min_order_qty=0.1,
            min_notional_usdt=5.0,
            price_tick_size=0.0001,
        )
    )


class NormalSecondLegSplitHelperTests(unittest.TestCase):
    def test_jtousdt_21_2_at_7044_returns_two_equal_intents(self) -> None:
        strategy = _long_bot_strategy()
        runtime_state = _runtime_with_rules()
        strategy_logs: list[tuple[str, dict]] = []

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=lambda event, payload: strategy_logs.append((event, dict(payload))),
        ):
            intents = strategy._maybe_build_normal_cycle_second_leg_split_intents(
                cycle_index=3,
                purpose="CYCLE_3_SHORT_REDUCE",
                qty=21.2,
                trigger_price=0.7044,
                snapshot=_snapshot(),
                runtime_state=runtime_state,
                side="short",
                position_idx=2,
                trigger_direction=2,
                metadata={"cycle_index": 3, "cycle_role": "short_reduce"},
            )

        self.assertIsNotNone(intents)
        assert intents is not None
        self.assertEqual(len(intents), 2)
        self.assertEqual([intent.qty for intent in intents], [10.6, 10.6])
        self.assertEqual(intents[0].trigger_price, 0.7044)
        self.assertEqual(intents[0].side, "short")
        self.assertTrue(all(intent.metadata.get("normal_cycle_second_leg_split") for intent in intents))
        events = [name for name, _ in strategy_logs]
        self.assertIn("fixed_cycle_normal_cycle_second_leg_split_created", events)

    def test_small_qty_returns_none(self) -> None:
        strategy = _long_bot_strategy()
        runtime_state = _runtime_with_rules()
        intents = strategy._maybe_build_normal_cycle_second_leg_split_intents(
            cycle_index=3,
            purpose="CYCLE_3_SHORT_REDUCE",
            qty=2.0,
            trigger_price=0.7044,
            snapshot=_snapshot(),
            runtime_state=runtime_state,
            side="short",
            position_idx=2,
            trigger_direction=2,
        )
        self.assertIsNone(intents)


class BuildShortTpFollowUpSplitTests(unittest.TestCase):
    def _long_bot_followup_state(self, *, cycle_index: int = 3, short_tp_qty: float = 21.2) -> dict:
        return {
            "cycle_waiting_for_short_tp": True,
            "short_tp_pending_cycle": cycle_index,
            "pending_short_cycle_index": cycle_index,
            "initial_short_qty": 84.8,
            "initial_long_qty": 67.2,
            "entry_reference_price": 0.71,
            "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
            "next_required_purpose": f"CYCLE_{cycle_index}_SHORT_REDUCE",
            "active_cycle_index": cycle_index,
            "current_short_cycle_index": 0,
            "current_long_cycle_index": cycle_index,
            "processed_cycle_purposes": [f"CYCLE_{cycle_index}_LONG_ADD"],
            "initial_entry_confirmed": True,
            "pending_cycle_loss_usdt": 0.0,
            "cycle_states": {
                str(cycle_index): {
                    "long_add_status": "PROCESSED",
                    "short_tp_status": "NONE",
                    "short_tp_qty": short_tp_qty,
                    "long_add_confirmed_pnl": -0.5,
                    "complete": False,
                }
            },
            "cycle_state": {
                "symbol": "JTOUSDT",
                "long_fills": {
                    str(cycle_index): {
                        "price": 0.71,
                        "incremental_qty": 21.2,
                        "closed_pnl": -0.5,
                    }
                },
                "short_fills": {},
                "long_cycle_index": cycle_index,
                "short_cycle_index": 0,
            },
        }

    def test_build_short_tp_follow_up_returns_split_intents(self) -> None:
        strategy = _long_bot_strategy()
        runtime_state = _runtime_with_rules(strategy_state=self._long_bot_followup_state())
        context = _context()
        snapshot = _snapshot(current_price=0.75)
        strategy_logs: list[tuple[str, dict]] = []

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=lambda event, payload: strategy_logs.append((event, dict(payload))),
        ), mock.patch.object(
            strategy,
            "_maybe_activate_recovery_after_first_leg_fill",
            return_value=False,
        ), mock.patch.object(
            strategy,
            "_can_submit_cycle_intent",
            return_value=(True, "ok", {}),
        ), mock.patch.object(
            strategy,
            "_fixed_short_cycle_qty",
            return_value=21.2,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

        self.assertGreaterEqual(len(intents), 2)
        self.assertAlmostEqual(sum(intent.qty for intent in intents), 21.2)
        self.assertTrue(all(intent.metadata.get("normal_cycle_second_leg_split") for intent in intents))
        self.assertEqual(intents[0].purpose, "CYCLE_3_SHORT_REDUCE")
        reasons = [
            payload.get("reason")
            for event, payload in strategy_logs
            if event == "fixed_cycle_normal_second_leg_split_disabled_for_short_reduce"
        ]
        self.assertEqual(reasons, [])
        split_created = [
            payload
            for event, payload in strategy_logs
            if event == "fixed_cycle_normal_cycle_second_leg_split_created"
        ]
        self.assertEqual(len(split_created), 1)

    def test_build_short_tp_follow_up_single_fallback_when_split_impossible(self) -> None:
        strategy = _long_bot_strategy()
        runtime_state = _runtime_with_rules(
            strategy_state=self._long_bot_followup_state(short_tp_qty=2.0)
        )
        context = _context()
        snapshot = _snapshot(current_price=0.75)
        strategy_logs: list[tuple[str, dict]] = []

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=lambda event, payload: strategy_logs.append((event, dict(payload))),
        ), mock.patch.object(
            strategy,
            "_maybe_activate_recovery_after_first_leg_fill",
            return_value=False,
        ), mock.patch.object(
            strategy,
            "_can_submit_cycle_intent",
            return_value=(True, "ok", {}),
        ), mock.patch.object(
            strategy,
            "_fixed_short_cycle_qty",
            return_value=2.0,
        ), mock.patch.object(
            strategy,
            "_maybe_build_normal_cycle_second_leg_split_intents",
            return_value=None,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0].qty, 2.0)
        disabled = [
            payload
            for event, payload in strategy_logs
            if event == "fixed_cycle_normal_second_leg_split_disabled_for_short_reduce"
        ]
        self.assertEqual(len(disabled), 1)
        self.assertEqual(disabled[0].get("reason"), "split_disabled_min_order_or_notional")
        self.assertEqual(disabled[0].get("qty"), 2.0)
        self.assertIn("min_order_qty", disabled[0])
        self.assertIn("min_notional_value", disabled[0])


class ShortBotLongReduceSplitRegressionTests(unittest.TestCase):
    def test_short_bot_long_reduce_split_still_works(self) -> None:
        strategy = _short_bot_strategy()
        runtime_state = _runtime_with_rules()
        intents = strategy._maybe_build_normal_cycle_second_leg_split_intents(
            cycle_index=2,
            purpose="CYCLE_2_LONG_REDUCE",
            qty=21.2,
            trigger_price=0.7044,
            snapshot=_snapshot(),
            runtime_state=runtime_state,
            side="long",
            position_idx=1,
            trigger_direction=1,
        )
        self.assertIsNotNone(intents)
        assert intents is not None
        self.assertEqual(len(intents), 2)
        self.assertEqual([intent.qty for intent in intents], [10.6, 10.6])
        self.assertEqual(intents[0].side, "long")


class ProfitStagingShortReduceTests(unittest.TestCase):
    def test_profit_staging_remains_blocked_for_short_reduce(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="JTOUSDT",
                restart=False,
                qty_step=0.1,
                min_order_qty=0.1,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                max_post_recovery_long_reduce_distance_pct=10.0,
            )
        )
        state = {
            "cycle_waiting_for_short_tp": True,
            "short_tp_pending_cycle": 3,
            "pending_short_cycle_index": 3,
            "initial_short_qty": 84.8,
            "initial_long_qty": 67.2,
            "entry_reference_price": 0.71,
            "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
            "next_required_purpose": "CYCLE_3_SHORT_REDUCE",
            "active_cycle_index": 3,
            "current_short_cycle_index": 0,
            "current_long_cycle_index": 3,
            "processed_cycle_purposes": ["CYCLE_3_LONG_ADD"],
            "initial_entry_confirmed": True,
            "pending_cycle_loss_usdt": 1.5,
            "cycle_states": {
                "3": {
                    "long_add_status": "PROCESSED",
                    "short_tp_status": "NONE",
                    "long_add_confirmed_pnl": -1.5,
                    "complete": False,
                }
            },
            "cycle_state": {
                "symbol": "JTOUSDT",
                "long_fills": {
                    "3": {
                        "price": 0.71,
                        "incremental_qty": 21.2,
                        "closed_pnl": -1.5,
                    }
                },
                "short_fills": {},
                "long_cycle_index": 3,
                "short_cycle_index": 0,
                "last_cycle_reference_price": 0.71,
            },
        }
        runtime_state = _runtime_with_rules(strategy_state=state)
        context = _context()
        snapshot = _snapshot(current_price=0.75)
        strategy_logs: list[tuple[str, dict]] = []

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=lambda event, payload: strategy_logs.append((event, dict(payload))),
        ), mock.patch.object(
            strategy,
            "_maybe_activate_recovery_after_first_leg_fill",
            return_value=False,
        ), mock.patch.object(
            strategy,
            "_can_submit_cycle_intent",
            return_value=(True, "ok", {}),
        ), mock.patch.object(
            strategy,
            "_fixed_short_cycle_qty",
            return_value=21.2,
        ), mock.patch.object(
            strategy,
            "_maybe_build_normal_cycle_second_leg_split_intents",
            return_value=None,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

        staged_disabled = [
            payload
            for event, payload in strategy_logs
            if event == "fixed_cycle_staged_second_leg_disabled_for_short_reduce"
        ]
        self.assertEqual(len(staged_disabled), 1)
        self.assertEqual(staged_disabled[0].get("reason"), "single_25pct_reduce_required")
        self.assertFalse(any(intent.metadata.get("is_staged_second_leg_tp") for intent in intents))


if __name__ == "__main__":
    unittest.main()
