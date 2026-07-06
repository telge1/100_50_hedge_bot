from __future__ import annotations

import unittest

from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState


class RecoveryBotStateTests(unittest.TestCase):
    def test_tracker_disabled_when_config_disabled(self) -> None:
        cfg = RecoveryBotConfig(enabled=False)
        tracker = RecoveryBotTracker(config=cfg)
        self.assertEqual(tracker.state, RecoveryState.DISABLED)

    def test_tracker_starts_waiting_for_trigger_when_enabled(self) -> None:
        cfg = RecoveryBotConfig(enabled=True)
        tracker = RecoveryBotTracker(config=cfg)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_TRIGGER)
        self.assertEqual(tracker.recovery_runs_for_trade, 0)

    def test_tracker_fields_initially_empty(self) -> None:
        cfg = RecoveryBotConfig(enabled=True)
        tracker = RecoveryBotTracker(config=cfg)
        self.assertIsNone(tracker.trigger_purpose)
        self.assertIsNone(tracker.trigger_cycle_index)
        self.assertIsNone(tracker.trigger_fill_price)
        self.assertIsNone(tracker.trigger_candle_index)
        self.assertIsNone(tracker.recovery_start_price)
        self.assertIsNone(tracker.recovery_start_candle_index)
        self.assertIsNone(tracker.recovery_start_long_qty)
        self.assertIsNone(tracker.recovery_start_short_qty)
        self.assertIsNone(tracker.neutralization_anchor_price)
        self.assertIsNone(tracker.neutralization_start_net_long_qty)
        self.assertIsNone(tracker.neutralization_fixed_step_qty)
        self.assertEqual(tracker.neutralization_steps_done, 0)
        self.assertIsNone(tracker.pair_anchor_price)
        self.assertEqual(tracker.pair_reduction_steps_done, 0)
        self.assertIsNone(tracker.loss_budget_usdt)
        self.assertEqual(tracker.loss_budget_used_usdt, 0.0)
        self.assertFalse(tracker.minimum_pair_reached)
        self.assertIsNone(tracker.final_exit_reason)
        self.assertIsNone(tracker.remaining_long_qty)
        self.assertIsNone(tracker.remaining_short_qty)
        self.assertIsNone(tracker.last_action_candle_index)


if __name__ == "__main__":
    unittest.main()

