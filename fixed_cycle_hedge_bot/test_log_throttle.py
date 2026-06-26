#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
    _log_event,
)
from fixed_cycle_hedge_bot import log_throttle
from fixed_cycle_hedge_bot.models import RuntimeState


class LogThrottleHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        log_throttle._MODULE_THROTTLE_BUCKETS.clear()

    def test_identical_event_logged_once_within_window(self) -> None:
        throttle_state: dict = {}
        payload = {
            "symbol": "JTOUSDT",
            "trade_block_id": "tb-1",
            "cycle_index": 2,
            "reason": "already_triggered",
        }
        event = "fixed_cycle_time_distance_refill_already_triggered_skipped"
        now = 1_000.0

        first = log_throttle.should_log_throttled_event(
            event, payload, throttle_state, now=now, interval_sec=60
        )
        second = log_throttle.should_log_throttled_event(
            event, payload, throttle_state, now=now + 10, interval_sec=60
        )

        self.assertTrue(first.should_log)
        self.assertFalse(second.should_log)
        self.assertEqual(second.suppressed_count, 1)

    def test_suppressed_count_accumulates_until_interval_expires(self) -> None:
        throttle_state: dict = {}
        payload = {"symbol": "JTOUSDT", "cycle_index": 2, "reason": "wait"}
        event = "fixed_cycle_second_leg_pending_skip_rebuild"
        now = 2_000.0

        log_throttle.should_log_throttled_event(event, payload, throttle_state, now=now, interval_sec=60)
        third = log_throttle.should_log_throttled_event(
            event, payload, throttle_state, now=now + 20, interval_sec=60
        )
        fourth = log_throttle.should_log_throttled_event(
            event, payload, throttle_state, now=now + 40, interval_sec=60
        )
        release = log_throttle.should_log_throttled_event(
            event, payload, throttle_state, now=now + 60, interval_sec=60
        )

        self.assertEqual(third.suppressed_count, 1)
        self.assertEqual(fourth.suppressed_count, 2)
        self.assertTrue(release.should_log)
        self.assertEqual(release.suppressed_count, 2)

    def test_signature_change_logs_immediately(self) -> None:
        throttle_state: dict = {}
        event = "fixed_cycle_time_distance_refill_already_triggered_skipped"
        now = 3_000.0
        log_throttle.should_log_throttled_event(
            event,
            {"symbol": "JTOUSDT", "cycle_index": 2, "reason": "wait"},
            throttle_state,
            now=now,
            interval_sec=60,
        )
        changed = log_throttle.should_log_throttled_event(
            event,
            {"symbol": "JTOUSDT", "cycle_index": 3, "reason": "wait"},
            throttle_state,
            now=now + 5,
            interval_sec=60,
        )
        self.assertTrue(changed.should_log)
        self.assertEqual(changed.suppressed_count, 0)

    def test_order_submitted_never_suppressed(self) -> None:
        throttle_state: dict = {}
        now = 4_000.0
        for offset in (0, 1, 2, 3):
            decision = log_throttle.should_log_throttled_event(
                "order_submitted",
                {"symbol": "JTOUSDT", "order_id": "x"},
                throttle_state,
                now=now + offset,
                interval_sec=60,
            )
            self.assertTrue(decision.should_log)

    def test_recovery_wallet_transfer_never_suppressed(self) -> None:
        throttle_state: dict = {}
        now = 5_000.0
        for offset in (0, 1, 2):
            decision = log_throttle.should_log_throttled_event(
                "fixed_cycle_recovery_wallet_transfer_success",
                {"symbol": "JTOUSDT", "transfer_id": "t1"},
                throttle_state,
                now=now + offset,
                interval_sec=60,
            )
            self.assertTrue(decision.should_log)


class StrategyLogEventRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        log_throttle._MODULE_THROTTLE_BUCKETS.clear()

    def test_debug_only_events_use_logger_debug(self) -> None:
        with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger") as strategy_logger:
            _log_event(
                "fixed_cycle_downside_cycle_intent_build_attempt",
                {"symbol": "JTOUSDT", "reason": "tick"},
            )
        strategy_logger.debug.assert_called_once()
        strategy_logger.info.assert_not_called()

    def test_refill_gate_before_equals_after_is_debug(self) -> None:
        gate_payload = {
            "symbol": "JTOUSDT",
            "before": {"refill_required": False, "active_cycle_index": 2},
            "after": {"refill_required": False, "active_cycle_index": 2},
        }
        with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger") as strategy_logger:
            _log_event("fixed_cycle_refill_gate_state_after_reconcile", gate_payload)
        strategy_logger.debug.assert_called_once()
        strategy_logger.info.assert_not_called()

    def test_refill_gate_before_not_equal_after_is_info(self) -> None:
        gate_payload = {
            "symbol": "JTOUSDT",
            "before": {"refill_required": False, "active_cycle_index": 2},
            "after": {"refill_required": True, "active_cycle_index": 2},
        }
        with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger") as strategy_logger:
            _log_event("fixed_cycle_refill_gate_state_after_reconcile", gate_payload)
        strategy_logger.info.assert_called_once()
        strategy_logger.debug.assert_not_called()

    def test_throttled_event_suppresses_repeat_info(self) -> None:
        runtime_state = RuntimeState(strategy_state={})
        payload = {
            "symbol": "JTOUSDT",
            "trade_block_id": "tb-1",
            "cycle_index": 2,
            "reason": "waiting",
        }
        with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy.logger") as strategy_logger:
            _log_event(
                "fixed_cycle_second_leg_pending_skip_rebuild",
                payload,
                runtime_state=runtime_state,
            )
            _log_event(
                "fixed_cycle_second_leg_pending_skip_rebuild",
                payload,
                runtime_state=runtime_state,
            )
        self.assertEqual(strategy_logger.info.call_count, 1)


class AuditThrottleTests(unittest.TestCase):
    def test_short_tp_follow_up_skip_is_throttled(self) -> None:
        runtime_state = RuntimeState(strategy_state={})
        audit = AuditLogger(
            logging.getLogger("test_log_throttle_audit"),
            runtime_state=runtime_state,
            extra_fields={"bot_name": "short_bot_1"},
        )
        payload = {
            "reason": "cycle_waiting_for_short_tp_false",
            "symbol": "JTOUSDT",
            "cycle_index": 2,
            "purpose": "CYCLE_2_LONG_REDUCE",
        }
        with mock.patch.object(audit.logger, "info") as info_mock:
            audit.log_event("fixed_cycle_short_tp_follow_up_skip", **payload)
            audit.log_event("fixed_cycle_short_tp_follow_up_skip", **payload)
        self.assertEqual(info_mock.call_count, 1)


class RecoverCycleSequenceLoggingTests(unittest.TestCase):
    def _strategy(self) -> FixedCycleHedgeStrategy:
        return FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="JTOUSDT",
                restart=False,
            )
        )

    def test_recover_logs_only_on_real_state_change(self) -> None:
        strategy = self._strategy()
        state = {
            "active_cycle_index": 2,
            "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
            "next_required_purpose": "CYCLE_2_SHORT_REDUCE",
            "last_completed_purpose": "CYCLE_2_LONG_ADD",
            "processed_cycle_purposes": ["CYCLE_2_LONG_ADD"],
        }
        runtime_state = RuntimeState(strategy_state=state)
        with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_mock:
            first = strategy._recover_cycle_sequence_state(runtime_state, state)
            second = strategy._recover_cycle_sequence_state(runtime_state, state)
        self.assertFalse(first)
        self.assertFalse(second)
        log_mock.assert_not_called()

    def test_short_bot_symmetric_recover_behavior(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="short_bot_1",
                strategy_side="short",
                symbol="JTOUSDT",
                restart=False,
            )
        )
        state = {
            "active_cycle_index": 2,
            "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
            "next_required_purpose": "CYCLE_2_LONG_REDUCE",
            "last_completed_purpose": "CYCLE_2_SHORT_REDUCE",
            "processed_cycle_purposes": ["CYCLE_2_SHORT_REDUCE"],
        }
        runtime_state = RuntimeState(strategy_state=state)
        with mock.patch("fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event") as log_mock:
            strategy._recover_cycle_sequence_state(runtime_state, state)
        log_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
