from __future__ import annotations

import copy
import unittest
from unittest import mock

from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.events import (
    maybe_activate_recovery,
    observe_recovery_trigger_fills,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState
from fixed_cycle_hedge_bot.models import FillEvent


class RecoveryBotTriggerIntegrationTests(unittest.TestCase):
    def _build_minimal_candles(self) -> list[dict[str, float]]:
        # Minimal 5m candle series with dummy OHLC values. Timestamps are
        # omitted; SyntheticCandle.from_row will tolerate missing timestamp.
        return [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.0, "high": 101.0, "low": 98.0, "close": 99.0},
            {"open": 98.0, "high": 100.0, "low": 97.0, "close": 98.0},
        ]

    @mock.patch("research.backtests.historical_backtest.ensure_recovery_exclusive_order_state")
    @mock.patch("research.backtests.historical_backtest.maybe_execute_neutralization_step")
    @mock.patch("research.backtests.historical_backtest.maybe_activate_recovery")
    @mock.patch("research.backtests.historical_backtest.observe_recovery_trigger_fills")
    def test_recovery_state_transitions_and_no_side_effects(
        self,
        mock_observe: mock.MagicMock,
        mock_activate: mock.MagicMock,
        mock_neutralize: mock.MagicMock,
        mock_freeze: mock.MagicMock,
    ) -> None:
        candles = self._build_minimal_candles()
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_3_SHORT_REDUCE",
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
        )

        # Capture tracker state transitions inside the patched hooks.
        states: list[RecoveryState] = []

        def _observe_side_effect(tracker, *, fills, candle_index: int) -> bool:  # type: ignore[override]
            if tracker is None:
                return False
            # Simulate TRIGGER_OBSERVED without touching simulator/backtest.
            tracker.state = RecoveryState.TRIGGER_OBSERVED
            states.append(tracker.state)
            return True

        def _activate_side_effect(
            tracker,
            *,
            current_price: float,
            candle_index: int,
            current_long_qty: float,
            current_short_qty: float,
        ) -> bool:  # type: ignore[override]
            if tracker is None:
                return False
            # Simulate transition into NEUTRALIZING without changing positions.
            tracker.state = RecoveryState.NEUTRALIZING
            states.append(tracker.state)
            return True

        mock_observe.side_effect = _observe_side_effect
        mock_activate.side_effect = _activate_side_effect
        mock_neutralize.return_value = []
        mock_freeze.return_value = 0

        # Baseline run without recovery config.
        baseline_result = run_historical_backtest(
            "BTCUSDT",
            "long",
            copy.deepcopy(candles),
            max_candles=2,
        )

        # Enabled config run with tracker present.
        enabled_result = run_historical_backtest(
            "BTCUSDT",
            "long",
            copy.deepcopy(candles),
            max_candles=2,
            recovery_bot_config=cfg,
        )

        # Hooks must have been called in the enabled run.
        self.assertTrue(mock_observe.called)
        self.assertTrue(mock_activate.called)
        # State sequence must have gone durch TRIGGER_OBSERVED -> NEUTRALIZING.
        self.assertIn(RecoveryState.TRIGGER_OBSERVED, states)
        self.assertIn(RecoveryState.NEUTRALIZING, states)

        # Positions, PnL und Logs müssen zwischen Baseline und Enabled vollständig
        # identisch sein, da Phase 2 keine Orders/Fills erzeugen darf.
        self.assertEqual(baseline_result.final_long_qty, enabled_result.final_long_qty)
        self.assertEqual(baseline_result.final_short_qty, enabled_result.final_short_qty)
        self.assertEqual(baseline_result.realized_pnl, enabled_result.realized_pnl)

        self.assertEqual(len(baseline_result.fill_log), len(enabled_result.fill_log))
        self.assertEqual(len(baseline_result.order_log), len(enabled_result.order_log))

        for left, right in zip(baseline_result.fill_log, enabled_result.fill_log):
            for key in (
                "purpose",
                "side",
                "qty",
                "fill_price",
                "closed_pnl",
                "long_qty_after",
                "short_qty_after",
            ):
                self.assertEqual(
                    left.get(key),
                    right.get(key),
                    msg=f"mismatch in fill_log field {key!r}",
                )

        for left, right in zip(baseline_result.order_log, enabled_result.order_log):
            for key in (
                "event_type",
                "purpose",
                "side",
                "qty",
                "price",
                "trigger_price",
                "reduce_only",
            ):
                self.assertEqual(
                    left.get(key),
                    right.get(key),
                    msg=f"mismatch in order_log field {key!r}",
                )

        # Sicherstellen, dass keine RECOVERY_-Purposes vorhanden sind.
        for row in enabled_result.fill_log:
            purpose = str(row.get("purpose") or "")
            self.assertFalse(
                purpose.startswith("RECOVERY_"),
                msg="unexpected RECOVERY_ purpose in fill_log",
            )
        for row in enabled_result.order_log:
            purpose = str(row.get("purpose") or "")
            self.assertFalse(
                purpose.startswith("RECOVERY_"),
                msg="unexpected RECOVERY_ purpose in order_log",
            )

    @mock.patch("research.backtests.historical_backtest.maybe_activate_recovery")
    @mock.patch("research.backtests.historical_backtest.observe_recovery_trigger_fills")
    def test_recovery_hooks_not_called_when_config_disabled(
        self,
        mock_observe: mock.MagicMock,
        mock_activate: mock.MagicMock,
    ) -> None:
        candles = self._build_minimal_candles()
        cfg = RecoveryBotConfig(enabled=False)

        run_historical_backtest(
            "BTCUSDT",
            "long",
            copy.deepcopy(candles),
            max_candles=2,
            recovery_bot_config=cfg,
        )

        # Disabled config must behave exactly as before: no tracker and no hook calls.
        self.assertFalse(mock_observe.called)
        self.assertFalse(mock_activate.called)

    def test_real_trigger_flow_with_fill_event(self) -> None:
        """End-to-end trigger flow using the real event hooks and FillEvent."""
        cfg = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_2_SHORT_REDUCE",
            trigger_wait_candles=1,
            trigger_price_drop_pct=1.0,
            neutralize_target_steps=5,
            available_profit_pool_usdt=10.0,
            loss_budget_profit_share_pct=20.0,
        )
        tracker = RecoveryBotTracker(config=cfg)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_TRIGGER)

        # Simulate a real CYCLE_2_SHORT_REDUCE fill at price 100.0 on candle 10.
        trigger_price = 100.0
        trigger_candle_index = 10
        fill = FillEvent(
            exchange_order_id="ex-1",
            client_order_id="c-1",
            side="short",
            purpose="CYCLE_2_SHORT_REDUCE",
            exec_qty=1.0,
            exec_price=trigger_price,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
        )

        observed = observe_recovery_trigger_fills(
            tracker,
            fills=[fill],
            candle_index=trigger_candle_index,
        )
        self.assertTrue(observed)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)
        self.assertEqual(tracker.trigger_purpose, "CYCLE_2_SHORT_REDUCE")
        self.assertEqual(tracker.trigger_cycle_index, 2)
        self.assertEqual(tracker.trigger_fill_price, trigger_price)
        self.assertEqual(tracker.trigger_candle_index, trigger_candle_index)

        # Positions and PnL are controlled by the caller; make sure they are
        # not changed by the activation calls.
        long_qty = 120.0
        short_qty = 70.0
        realized_pnl = 0.0

        # Before wait-candles elapsed: candles_since_trigger = 0 < 1.
        activated = maybe_activate_recovery(
            tracker,
            current_price=trigger_price,
            candle_index=trigger_candle_index,
            current_long_qty=long_qty,
            current_short_qty=short_qty,
        )
        self.assertFalse(activated)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)
        self.assertEqual(long_qty, 120.0)
        self.assertEqual(short_qty, 70.0)
        self.assertEqual(realized_pnl, 0.0)

        # After wait-candles, but price drop still below 1% (0.5% drop).
        activated = maybe_activate_recovery(
            tracker,
            current_price=99.5,
            candle_index=trigger_candle_index + 1,
            current_long_qty=long_qty,
            current_short_qty=short_qty,
        )
        self.assertFalse(activated)
        self.assertEqual(tracker.state, RecoveryState.TRIGGER_OBSERVED)

        # Now wait-candles satisfied and price drop >= 1%.
        activated = maybe_activate_recovery(
            tracker,
            current_price=98.9,  # 1.1% unter Trigger-Preis
            candle_index=trigger_candle_index + 1,
            current_long_qty=long_qty,
            current_short_qty=short_qty,
        )
        self.assertTrue(activated)
        self.assertEqual(tracker.state, RecoveryState.NEUTRALIZING)
        self.assertEqual(tracker.recovery_runs_for_trade, 1)
        self.assertEqual(tracker.recovery_start_long_qty, long_qty)
        self.assertEqual(tracker.recovery_start_short_qty, short_qty)
        self.assertAlmostEqual(
            tracker.neutralization_start_net_long_qty,
            max(long_qty - short_qty, 0.0),
            places=6,
        )
        # Profit pool 10 USDT bei 20 % → 2 USDT Budget.
        self.assertAlmostEqual(tracker.loss_budget_usdt or 0.0, 2.0, places=6)
        self.assertEqual(tracker.loss_budget_used_usdt, 0.0)

        # No Recovery orders/fills are created by the hooks themselves.
        fill_log: list[dict[str, float]] = []
        order_log: list[dict[str, float]] = []
        self.assertEqual(len(fill_log), 0)
        self.assertEqual(len(order_log), 0)

        # Positions and PnL remain unchanged by the transition.
        self.assertEqual(long_qty, 120.0)
        self.assertEqual(short_qty, 70.0)
        self.assertEqual(realized_pnl, 0.0)


if __name__ == "__main__":
    unittest.main()


