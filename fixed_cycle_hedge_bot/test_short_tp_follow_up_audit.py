#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import (
    STEP_WAITING_FOR_PAIR_FIRST_LEG,
    STEP_WAITING_FOR_PAIR_SECOND_LEG,
)
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _snapshot() -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="JTOUSDT",
        current_price=0.74,
        long_qty=67.2,
        short_qty=134.4,
        long_avg=0.744,
        short_avg=0.7438,
    )


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_short_tp_follow_up_audit")),
        runtime_name="test_runtime",
        symbol="JTOUSDT",
        category="linear",
        min_order_value=5.0,
    )


def _stale_second_leg_state() -> dict:
    return {
        "cycle_waiting_for_short_tp": True,
        "short_tp_pending_cycle": 1,
        "cycle_waiting_for_long_reduce": True,
        "long_reduce_pending_cycle": 1,
        "pending_short_cycle_index": 1,
        "pending_long_cycle_index": 1,
        "refill_pending": True,
        "refill_in_progress": True,
        "post_refill_structure_rebuild_required": True,
        "cycle_states": {
            "1": {
                "long_add_confirmed_pnl": -0.5,
                "short_reduce_status": "PROCESSED",
                "long_reduce_status": "NONE",
                "complete": False,
            }
        },
        "processed_cycle_purposes": ["CYCLE_1_SHORT_REDUCE"],
        "cycle_completed_count": 2,
        "cycle_pair_count": 2,
        "active_cycle_index": 1,
        "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
        "next_required_purpose": "CYCLE_1_LONG_REDUCE",
        "initial_entry_confirmed": True,
        "initial_structure_built": True,
        "initial_long_qty": 67.2,
        "initial_short_qty": 134.4,
        "entry_reference_price": 0.74,
    }


class DuplicatePendingCycleLossAuditTests(unittest.TestCase):
    def test_emit_short_tp_follow_up_skip_accepts_duplicate_extra_key(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy()
        state = _stale_second_leg_state()
        state["cycle_states"] = {
            "1": {
                "long_add_confirmed_pnl": None,
                "short_reduce_status": "PROCESSED",
                "long_reduce_status": "NONE",
                "complete": False,
            }
        }
        state["refill_pending"] = False
        state["refill_in_progress"] = False
        runtime_state = RuntimeState(
            strategy_state=state,
            last_snapshot=_snapshot(),
        )
        context = _context()
        captured: list[dict] = []

        def _capture_log_event(event: str, **payload: object) -> None:
            captured.append({"event": event, **payload})

        context.audit.log_event = _capture_log_event  # type: ignore[method-assign]

        intents = strategy._build_short_tp_follow_up(_snapshot(), runtime_state, context)

        self.assertEqual(intents, [])
        skip_events = [row for row in captured if row.get("event") == "fixed_cycle_short_tp_follow_up_skip"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0].get("reason"), "long_reduce_blocked_until_confirmed_pnl")
        self.assertIn("pending_cycle_loss_usdt", skip_events[0])
        self.assertEqual(len([k for k in skip_events[0] if k == "pending_cycle_loss_usdt"]), 1)


class SecondLegFollowupStateResetTests(unittest.TestCase):
    def _assert_fresh_first_leg_state(self, strategy: FixedCycleHedgeStrategy, state: dict) -> None:
        self.assertEqual(state.get("cycle_step"), STEP_WAITING_FOR_PAIR_FIRST_LEG)
        self.assertFalse(state.get("cycle_waiting_for_short_tp"))
        self.assertEqual(int(state.get("short_tp_pending_cycle") or 0), 0)
        self.assertFalse(state.get("cycle_waiting_for_long_reduce"))
        self.assertEqual(int(state.get("long_reduce_pending_cycle") or 0), 0)
        self.assertEqual(int(state.get("pending_short_cycle_index") or 0), 0)
        self.assertEqual(int(state.get("pending_long_cycle_index") or 0), 0)
        self.assertFalse(state.get("refill_pending"))
        self.assertFalse(state.get("refill_in_progress"))
        self.assertFalse(state.get("post_refill_structure_rebuild_required"))

    def test_reset_exit_state_for_new_structure_clears_stale_short_bot_flags(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy()
        runtime_state = RuntimeState(
            strategy_state=_stale_second_leg_state(),
            last_snapshot=_snapshot(),
        )
        with mock.patch.object(strategy, "_set_final_exit_missing_block"):
            strategy._reset_exit_state_for_new_structure(
                runtime_state,
                "initial_entry_confirmed",
            )
        state = runtime_state.strategy_state
        self._assert_fresh_first_leg_state(strategy, state)
        self.assertEqual(state.get("next_required_purpose"), "CYCLE_1_SHORT_REDUCE")
        self.assertEqual(state.get("cycle_states"), {})

    def test_reset_exit_state_for_new_structure_clears_stale_long_bot_flags(self) -> None:
        strategy = FixedCycleHedgeStrategy()
        runtime_state = RuntimeState(
            strategy_state=_stale_second_leg_state(),
            last_snapshot=_snapshot(),
        )
        with mock.patch.object(strategy, "_set_final_exit_missing_block"):
            strategy._reset_exit_state_for_new_structure(
                runtime_state,
                "initial_entry_confirmed",
            )
        state = runtime_state.strategy_state
        self._assert_fresh_first_leg_state(strategy, state)
        self.assertEqual(state.get("next_required_purpose"), "CYCLE_1_LONG_ADD")
        self.assertEqual(state.get("cycle_states"), {})

    def test_reset_after_full_exit_for_next_trade_clears_stale_flags(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy()
        runtime_state = RuntimeState(
            strategy_state=_stale_second_leg_state(),
            last_snapshot=_snapshot(),
        )
        flat_snapshot = HedgeSnapshot(
            symbol="JTOUSDT",
            current_price=0.74,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
        )
        with mock.patch.object(strategy, "_persist_cycle_sequence_state"):
            strategy._reset_after_full_exit_for_next_trade(
                runtime_state,
                flat_snapshot,
                reason="full_exit",
            )
        state = runtime_state.strategy_state
        self._assert_fresh_first_leg_state(strategy, state)
        self.assertEqual(state.get("next_required_purpose"), "CYCLE_1_SHORT_REDUCE")


class ShortBotFreshEntrySequenceTests(unittest.TestCase):
    def test_follow_up_skips_until_first_leg_after_fresh_structure_reset(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy()
        runtime_state = RuntimeState(
            strategy_state=_stale_second_leg_state(),
            last_snapshot=_snapshot(),
        )
        with mock.patch.object(strategy, "_set_final_exit_missing_block"):
            strategy._reset_exit_state_for_new_structure(
                runtime_state,
                "initial_entry_confirmed",
            )
        state = runtime_state.strategy_state
        self.assertEqual(state.get("next_required_purpose"), "CYCLE_1_SHORT_REDUCE")

        captured: list[dict] = []
        context = _context()
        context.audit.log_event = lambda event, **payload: captured.append(  # type: ignore[method-assign]
            {"event": event, **payload}
        )

        intents = strategy._build_short_tp_follow_up(_snapshot(), runtime_state, context)
        self.assertEqual(intents, [])
        skip_events = [row for row in captured if row.get("event") == "fixed_cycle_short_tp_follow_up_skip"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0].get("reason"), "sequence_waiting_for_first_leg")
        purposes = [intent.purpose for intent in intents]
        self.assertNotIn("CYCLE_1_LONG_REDUCE", purposes)


class LongBotFreshEntryRegressionTests(unittest.TestCase):
    def test_follow_up_skips_until_first_leg_after_fresh_structure_reset(self) -> None:
        strategy = FixedCycleHedgeStrategy()
        runtime_state = RuntimeState(
            strategy_state=_stale_second_leg_state(),
            last_snapshot=_snapshot(),
        )
        with mock.patch.object(strategy, "_set_final_exit_missing_block"):
            strategy._reset_exit_state_for_new_structure(
                runtime_state,
                "initial_entry_confirmed",
            )
        state = runtime_state.strategy_state
        self.assertEqual(state.get("next_required_purpose"), "CYCLE_1_LONG_ADD")

        captured: list[dict] = []
        context = _context()
        context.audit.log_event = lambda event, **payload: captured.append(  # type: ignore[method-assign]
            {"event": event, **payload}
        )

        intents = strategy._build_short_tp_follow_up(_snapshot(), runtime_state, context)
        self.assertEqual(intents, [])
        skip_events = [row for row in captured if row.get("event") == "fixed_cycle_short_tp_follow_up_skip"]
        self.assertEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0].get("reason"), "sequence_waiting_for_first_leg")


if __name__ == "__main__":
    unittest.main()
