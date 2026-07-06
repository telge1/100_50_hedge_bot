from __future__ import annotations

from decimal import Decimal
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_NEUTRALIZE_LONG_PURPOSE,
    ensure_recovery_exclusive_order_state,
    maybe_execute_neutralization_step,
    validate_recovery_mode_exclusivity,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState
from research.backtests.simulated_order_book import SyntheticCandle
from research.backtests.stuck_recovery_reload import StuckRecoveryReloadConfig


class RecoveryBotNeutralizationIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _sim(self, *, close: float = 90.0) -> HedgeBotOriginalSimulator:
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=close)
        sim.book.long_qty = 120.0
        sim.book.short_qty = 70.0
        sim.book.long_avg = 100.0
        sim.book.short_avg = 100.0
        sim.book.fee_rate = 0.00055
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="test_setup", price=close)
        self._sims.append(sim)
        return sim

    def _tracker(self, *, anchor_price: float = 90.0) -> RecoveryBotTracker:
        cfg = RecoveryBotConfig(
            enabled=True,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
        )
        tracker = RecoveryBotTracker(config=cfg)
        tracker.state = RecoveryState.NEUTRALIZING
        tracker.neutralization_anchor_price = anchor_price
        tracker.neutralization_fixed_step_qty = 10.0
        tracker.loss_budget_usdt = 1_000.0
        return tracker

    def test_example_sequence_long_120_short_70_to_pair_reducing(self) -> None:
        sim = self._sim(close=90.0)
        tracker = self._tracker(anchor_price=90.0)
        prices = [90.00, 89.50, 89.10, 88.50, 88.20, 87.30, 86.40, 85.50]
        total_fills = 0
        for index, price in enumerate(prices, start=1):
            sim.candle = SyntheticCandle(symbol=sim.symbol, close=price)
            sim.candle_index = index
            sim._refresh_snapshot_from_book(source="example_sequence", price=price)
            fills = maybe_execute_neutralization_step(
                sim,
                tracker,
                current_price=price,
                candle_index=index,
            )
            total_fills += len(fills)

        self.assertEqual(total_fills, 5)
        self.assertAlmostEqual(sim.book.long_qty, 70.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 70.0, places=6)
        self.assertEqual(tracker.state, RecoveryState.PAIR_REDUCING)
        self.assertAlmostEqual(tracker.pair_anchor_price or 0.0, 85.5, places=6)

    def test_conflicting_normal_orders_are_cancelled_before_recovery_fill(self) -> None:
        sim = self._sim(close=89.1)
        tracker = self._tracker(anchor_price=90.0)

        normal_intent = StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_3_SHORT_REDUCE",
            price=120.0,
            order_type="Limit",
            reduce_only=True,
        )
        sim.submit_intents_to_book([normal_intent], event_source="test_setup")
        active_before = [str(order.purpose or "") for order in sim.book.active_orders()]
        self.assertIn("CYCLE_3_SHORT_REDUCE", active_before)

        fills = maybe_execute_neutralization_step(
            sim,
            tracker,
            current_price=89.1,
            candle_index=1,
        )

        self.assertEqual(len(fills), 1)
        active_after = [str(order.purpose or "") for order in sim.book.active_orders()]
        self.assertFalse(any(not purpose.startswith("RECOVERY_") for purpose in active_after))
        cancelled = [
            row for row in sim.order_log
            if str(row.get("event_type") or "") == "cancelled"
            and str(row.get("purpose") or "") == "CYCLE_3_SHORT_REDUCE"
        ]
        self.assertTrue(cancelled)

    def test_historical_backtest_logs_recovery_fill_without_recovery_active_orders(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 90.0, "high": 90.0, "low": 89.1, "close": 89.1},
            {"open": 89.1, "high": 89.1, "low": 88.2, "close": 88.2},
        ]
        cfg = RecoveryBotConfig(enabled=True)

        with mock.patch("research.backtests.historical_backtest.observe_recovery_trigger_fills") as mock_observe, mock.patch(
            "research.backtests.historical_backtest.maybe_activate_recovery"
        ) as mock_activate, mock.patch(
            "research.backtests.historical_backtest.HedgeBotOriginalSimulator"
        ) as mock_sim_cls:
            sim = self._sim(close=90.0)
            tracker = self._tracker(anchor_price=90.0)
            sim.recovery_bot_tracker = tracker
            sim.recovery_bot_config = cfg
            sim.candle = SyntheticCandle(symbol=sim.symbol, close=89.1)
            sim.candle_index = 1

            def _entry_smoke():
                return type(
                    "EntryResult",
                    (),
                    {
                        "entry_fills": [],
                    },
                )()

            def _process_candle(candle, **_kwargs):
                sim.candle = candle
                return type(
                    "CandleResult",
                    (),
                    {
                        "same_candle_fill_count": 0,
                        "paired_exit_fills_count": 0,
                        "candle_fills": [],
                    },
                )()

            sim.run_entry_smoke = _entry_smoke  # type: ignore[assignment]
            sim.process_candle = _process_candle  # type: ignore[assignment]
            sim.stuck_recovery_reload_tracker = None
            mock_sim_cls.return_value = sim
            mock_observe.side_effect = lambda *args, **kwargs: False
            mock_activate.side_effect = lambda *args, **kwargs: False

            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=2,
                recovery_bot_config=cfg,
            )

        recovery_fills = [
            row for row in result.fill_log if str(row.get("purpose") or "") == RECOVERY_NEUTRALIZE_LONG_PURPOSE
        ]
        self.assertTrue(recovery_fills)
        self.assertFalse(
            any(str(row.get("purpose") or "").startswith("RECOVERY_") for row in result.final_active_orders)
        )

    def test_frozen_recovery_skips_normal_process_candle_and_only_allows_recovery_fill(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 90.0, "high": 120.0, "low": 89.1, "close": 89.1},
        ]
        cfg = RecoveryBotConfig(enabled=True)
        sim = self._sim(close=90.0)
        tracker = self._tracker(anchor_price=90.0)
        sim.recovery_bot_tracker = tracker
        sim.recovery_bot_config = cfg

        # Add a normal order that would be fillable if the normal strategy path ran.
        normal_intent = StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_3_SHORT_REDUCE",
            price=120.0,
            order_type="Limit",
            reduce_only=True,
        )
        sim.submit_intents_to_book([normal_intent], event_source="test_setup")
        before_long = sim.book.long_qty
        before_short = sim.book.short_qty

        original_process = sim.process_candle
        process_calls = {"count": 0}

        def _wrapped_process(*args, **kwargs):
            process_calls["count"] += 1
            return original_process(*args, **kwargs)

        sim.process_candle = _wrapped_process  # type: ignore[assignment]

        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]
            sim.stuck_recovery_reload_tracker = None
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=1,
                recovery_bot_config=cfg,
            )

        self.assertEqual(process_calls["count"], 0)
        normal_fills = [
            row for row in result.fill_log if str(row.get("purpose") or "") == "CYCLE_3_SHORT_REDUCE"
        ]
        self.assertFalse(normal_fills)
        recovery_fills = [
            row for row in result.fill_log if str(row.get("purpose") or "") == RECOVERY_NEUTRALIZE_LONG_PURPOSE
        ]
        self.assertEqual(len(recovery_fills), 1)
        self.assertAlmostEqual(before_short, sim.book.short_qty, places=6)
        self.assertLess(sim.book.long_qty, before_long)

    def test_multiple_frozen_candles_do_not_create_cancel_loop_or_normal_orders(self) -> None:
        sim = self._sim(close=90.0)
        tracker = self._tracker(anchor_price=90.0)
        sim.recovery_bot_tracker = tracker

        normal_intent = StrategyIntent(
            side="short",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            price=120.0,
            order_type="Limit",
            reduce_only=True,
        )
        sim.submit_intents_to_book([normal_intent], event_source="test_setup")
        cancelled_once = ensure_recovery_exclusive_order_state(sim, tracker)
        self.assertEqual(cancelled_once, 1)

        cancel_rows_after_first = len(
            [row for row in sim.order_log if str(row.get("event_type") or "") == "cancelled"]
        )

        # Several candles without 1% drop: no new normal orders, no recurring cancel loop.
        for index, price in enumerate([89.5, 89.4, 89.3], start=1):
            sim.candle = SyntheticCandle(symbol=sim.symbol, close=price)
            sim.candle_index = index
            sim._refresh_snapshot_from_book(source="frozen_loop", price=price)
            fills = maybe_execute_neutralization_step(
                sim,
                tracker,
                current_price=price,
                candle_index=index,
            )
            self.assertEqual(fills, [])

        cancel_rows_after_loop = len(
            [row for row in sim.order_log if str(row.get("event_type") or "") == "cancelled"]
        )
        self.assertEqual(cancel_rows_after_first, cancel_rows_after_loop)
        submitted_non_recovery = [
            row
            for row in sim.order_log
            if str(row.get("event_type") or "") == "submitted"
            and not str(row.get("purpose") or "").startswith("RECOVERY_")
        ]
        # Only the original manually seeded normal order exists.
        self.assertEqual(len(submitted_non_recovery), 1)

    def test_mutual_exclusivity_with_stuck_reload(self) -> None:
        with self.assertRaises(ValueError):
            validate_recovery_mode_exclusivity(
                recovery_bot_config=RecoveryBotConfig(enabled=True),
                stuck_recovery_reload_config=StuckRecoveryReloadConfig(enabled=True),
            )

    def test_qty_step_rounding_example_respects_tolerance(self) -> None:
        sim = self._sim(close=89.1)
        sim.book.long_qty = 70.4
        sim.book.short_qty = 70.0
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="rounding_setup", price=89.1)
        sim.runtime_state.instrument_rules[sim.symbol]["qty_step"] = Decimal("0.3")
        tracker = self._tracker(anchor_price=90.0)
        fills = maybe_execute_neutralization_step(
            sim,
            tracker,
            current_price=89.1,
            candle_index=1,
        )
        self.assertEqual(len(fills), 1)
        self.assertGreaterEqual(sim.book.long_qty, sim.book.short_qty)


if __name__ == "__main__":
    unittest.main()

