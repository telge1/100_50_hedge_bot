from __future__ import annotations

import unittest

from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.events import (
    attach_recovery_bot_tracker,
    maybe_activate_recovery,
    observe_recovery_trigger_fills,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState


class _DummyFill:
    def __init__(self, purpose: str, exec_price: float) -> None:
        self.purpose = purpose
        self.exec_price = exec_price


class RecoveryBotEventsTests(unittest.TestCase):
    def test_attach_tracker_none_and_disabled(self) -> None:
        self.assertIsNone(attach_recovery_bot_tracker(None))
        cfg_disabled = RecoveryBotConfig(enabled=False)
        self.assertIsNone(attach_recovery_bot_tracker(cfg_disabled))

    def test_attach_tracker_enabled_waiting_for_trigger(self) -> None:
        cfg = RecoveryBotConfig(enabled=True)
        tracker = attach_recovery_bot_tracker(cfg)
        self.assertIsNotNone(tracker)
        assert tracker is not None
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_TRIGGER)

    def test_similar_purpose_does_not_trigger(self) -> None:
        cfg = RecoveryBotConfig(enabled=True, trigger_order="CYCLE_3_SHORT_REDUCE")
        tracker = RecoveryBotTracker(config=cfg)
        fills = [_DummyFill("CYCLE_3_LONG_REDUCE", 100.0)]
        changed = observe_recovery_trigger_fills(tracker, fills=fills, candle_index=1)
        self.assertFalse(changed)
        self.assertIsNone(tracker.trigger_purpose)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_TRIGGER)

    def test_exact_fill_triggers_once_and_records_fields(self) -> None:
        cfg = RecoveryBotConfig(enabled=True, trigger_order="CYCLE_2_SHORT_REDUCE")
        tracker = RecoveryBotTracker(config=cfg)
        fills = [_DummyFill("CYCLE_2_SHORT_REDUCE", 95.0)]
        changed = observe_recovery_trigger_fills(tracker, fills=fills, candle_index=5)
        self.assertTrue(changed)
        self.assertEqual(tracker.trigger_purpose, "CYCLE_2_SHORT_REDUCE")
        self.assertEqual(tracker.trigger_cycle_index, 2)
        self.assertEqual(tracker.trigger_fill_price, 95.0)
        self.assertEqual(tracker.trigger_candle_index, 5)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)

        # Second call with the same fill must not change state or double-count.
        changed_again = observe_recovery_trigger_fills(
            tracker,
            fills=fills,
            candle_index=5,
        )
        self.assertFalse(changed_again)
        self.assertEqual(tracker.trigger_candle_index, 5)

    def test_no_activation_before_wait_candles_and_price_drop(self) -> None:
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_3_SHORT_REDUCE",
            trigger_wait_candles=2,
            trigger_price_drop_pct=10.0,
        )
        tracker = RecoveryBotTracker(config=cfg)
        trigger_fill = _DummyFill("CYCLE_3_SHORT_REDUCE", 100.0)
        observe_recovery_trigger_fills(tracker, fills=[trigger_fill], candle_index=5)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)

        # Too few candles since trigger.
        activated = maybe_activate_recovery(
            tracker,
            current_price=85.0,
            candle_index=6,
            current_long_qty=0.0,
            current_short_qty=0.0,
        )
        self.assertFalse(activated)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)

        # Enough candles but insufficient price drop.
        activated = maybe_activate_recovery(
            tracker,
            current_price=92.0,  # 8% drop only
            candle_index=7,
            current_long_qty=0.0,
            current_short_qty=0.0,
        )
        self.assertFalse(activated)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)

    def test_activation_when_both_wait_and_drop_met(self) -> None:
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_3_SHORT_REDUCE",
            trigger_wait_candles=1,
            trigger_price_drop_pct=5.0,
            neutralize_target_steps=5,
        )
        tracker = RecoveryBotTracker(config=cfg)
        trigger_fill = _DummyFill("CYCLE_3_SHORT_REDUCE", 100.0)
        observe_recovery_trigger_fills(tracker, fills=[trigger_fill], candle_index=5)

        activated = maybe_activate_recovery(
            tracker,
            current_price=94.0,  # 6% drop
            candle_index=6,
            current_long_qty=120.0,
            current_short_qty=70.0,
        )
        self.assertTrue(activated)
        self.assertEqual(tracker.state, RecoveryState.NEUTRALIZING)
        self.assertEqual(tracker.recovery_runs_for_trade, 1)
        self.assertEqual(tracker.recovery_start_price, 94.0)
        self.assertEqual(tracker.recovery_start_candle_index, 6)
        self.assertEqual(tracker.recovery_start_long_qty, 120.0)
        self.assertEqual(tracker.recovery_start_short_qty, 70.0)
        self.assertEqual(tracker.neutralization_anchor_price, 94.0)
        # Net long 50, 5 steps => 10 per step.
        self.assertAlmostEqual(tracker.neutralization_start_net_long_qty, 50.0, places=6)
        self.assertAlmostEqual(tracker.neutralization_fixed_step_qty, 10.0, places=6)
        # Budget must be computed once at start.
        self.assertIsNotNone(tracker.loss_budget_usdt)
        self.assertEqual(tracker.loss_budget_used_usdt, 0.0)

    def test_zero_wait_or_zero_drop_activate_immediately(self) -> None:
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_2_SHORT_REDUCE",
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
        )
        tracker = RecoveryBotTracker(config=cfg)
        trigger_fill = _DummyFill("CYCLE_2_SHORT_REDUCE", 100.0)
        observe_recovery_trigger_fills(tracker, fills=[trigger_fill], candle_index=5)

        activated = maybe_activate_recovery(
            tracker,
            current_price=100.0,
            candle_index=5,
            current_long_qty=10.0,
            current_short_qty=5.0,
        )
        self.assertTrue(activated)
        self.assertEqual(tracker.state, RecoveryState.NEUTRALIZING)

    def test_max_recovery_runs_prevents_additional_activation(self) -> None:
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_3_SHORT_REDUCE",
            max_recovery_runs_per_trade=1,
        )
        tracker = RecoveryBotTracker(config=cfg)
        trigger_fill = _DummyFill("CYCLE_3_SHORT_REDUCE", 100.0)
        observe_recovery_trigger_fills(tracker, fills=[trigger_fill], candle_index=5)

        first = maybe_activate_recovery(
            tracker,
            current_price=95.0,
            candle_index=5,
            current_long_qty=10.0,
            current_short_qty=0.0,
        )
        self.assertTrue(first)
        self.assertEqual(tracker.state, RecoveryState.NEUTRALIZING)

        # Simulate that we would like to trigger again; since the state is not
        # TRIGGER_OBSERVED anymore, activation must not happen.
        second = maybe_activate_recovery(
            tracker,
            current_price=90.0,
            candle_index=6,
            current_long_qty=10.0,
            current_short_qty=0.0,
        )
        self.assertFalse(second)
        self.assertEqual(tracker.recovery_runs_for_trade, 1)

    def test_zero_max_recovery_runs_prevents_activation(self) -> None:
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_3_SHORT_REDUCE",
            max_recovery_runs_per_trade=0,
        )
        tracker = RecoveryBotTracker(config=cfg)
        trigger_fill = _DummyFill("CYCLE_3_SHORT_REDUCE", 100.0)
        observe_recovery_trigger_fills(tracker, fills=[trigger_fill], candle_index=5)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)

        activated = maybe_activate_recovery(
            tracker,
            current_price=90.0,
            candle_index=6,
            current_long_qty=10.0,
            current_short_qty=0.0,
        )
        # With max_recovery_runs_per_trade=0 no run may be started.
        self.assertFalse(activated)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)
        self.assertEqual(tracker.recovery_runs_for_trade, 0)


if __name__ == "__main__":
    unittest.main()

