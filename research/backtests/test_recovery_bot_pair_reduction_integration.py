from __future__ import annotations

import unittest
from unittest import mock

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_PAIR_REDUCE_LONG_PURPOSE,
    RECOVERY_PAIR_REDUCE_SHORT_PURPOSE,
    maybe_advance_minimum_pair_state,
    maybe_execute_pair_reduction_step,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState
from research.backtests.simulated_order_book import SyntheticCandle


class RecoveryBotPairReductionIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _sim(
        self,
        *,
        close: float = 100.0,
        qty: float = 70.0,
        long_avg: float = 100.0,
        short_avg: float = 100.0,
    ) -> HedgeBotOriginalSimulator:
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=close)
        sim.book.long_qty = float(qty)
        sim.book.short_qty = float(qty)
        sim.book.long_avg = float(long_avg)
        sim.book.short_avg = float(short_avg)
        sim.book.fee_rate = 0.00055
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="pair_integration_setup", price=close)
        self._sims.append(sim)
        return sim

    def _tracker(
        self,
        *,
        anchor_price: float = 100.0,
        minimum_pair_qty: float = 20.0,
        loss_budget_usdt: float = 1_000.0,
    ) -> RecoveryBotTracker:
        tracker = RecoveryBotTracker(
            config=RecoveryBotConfig(
                enabled=True,
                pair_reduce_move_pct=1.0,
                pair_reduce_mode="fixed_qty",
                pair_reduce_qty=10.0,
                minimum_pair_qty=minimum_pair_qty,
            )
        )
        tracker.state = RecoveryState.PAIR_REDUCING
        tracker.pair_anchor_price = float(anchor_price)
        tracker.loss_budget_usdt = float(loss_budget_usdt)
        return tracker

    def test_controlled_example_reduces_70_to_20_and_then_advances_state(self) -> None:
        sim = self._sim(close=100.0, qty=70.0, long_avg=90.0, short_avg=110.0)
        tracker = self._tracker(anchor_price=100.0, minimum_pair_qty=20.0, loss_budget_usdt=1_000.0)
        prices = [100.00, 100.50, 101.00, 100.40, 99.99, 98.99, 99.98, 100.98]

        all_fills = []
        for index, price in enumerate(prices, start=1):
            sim.candle = SyntheticCandle(symbol=sim.symbol, close=price)
            sim.candle_index = index
            sim._refresh_snapshot_from_book(source="pair_example", price=price)
            all_fills.extend(
                maybe_execute_pair_reduction_step(
                    sim,
                    tracker,
                    current_price=price,
                    candle_index=index,
                )
            )

        self.assertEqual(len(all_fills), 10)
        self.assertAlmostEqual(sim.book.long_qty, 20.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 20.0, places=6)
        self.assertEqual(tracker.state, RecoveryState.MINIMUM_PAIR_REACHED)
        self.assertTrue(tracker.minimum_pair_reached)
        self.assertAlmostEqual(tracker.pair_anchor_price or 0.0, 100.98, places=6)

        advanced = maybe_advance_minimum_pair_state(sim, tracker, current_price=100.98)
        self.assertTrue(advanced)
        self.assertEqual(tracker.state, RecoveryState.READY_TO_CLOSE)

    def test_minimum_pair_can_advance_to_waiting_for_reload(self) -> None:
        sim = self._sim(close=90.0, qty=20.0, long_avg=100.0, short_avg=80.0)
        tracker = self._tracker(anchor_price=90.0, minimum_pair_qty=20.0, loss_budget_usdt=0.5)
        tracker.state = RecoveryState.MINIMUM_PAIR_REACHED
        tracker.minimum_pair_reached = True
        advanced = maybe_advance_minimum_pair_state(sim, tracker, current_price=90.0)
        self.assertTrue(advanced)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_RELOAD)

    def test_historical_backtest_keeps_normal_strategy_frozen_during_pair_reduction(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 101.0, "low": 100.0, "close": 101.0},
            {"open": 101.0, "high": 101.0, "low": 99.99, "close": 99.99},
        ]
        cfg = RecoveryBotConfig(enabled=True)
        sim = self._sim(close=100.0, qty=70.0)
        tracker = self._tracker(anchor_price=100.0, minimum_pair_qty=20.0)
        sim.recovery_bot_tracker = tracker
        sim.recovery_bot_config = cfg

        normal_intent = StrategyIntent(
            side="short",
            qty=1.0,
            purpose="CYCLE_3_SHORT_REDUCE",
            price=101.0,
            order_type="Limit",
            reduce_only=True,
        )
        sim.submit_intents_to_book([normal_intent], event_source="pair_reduction_setup")

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
                max_candles=2,
                recovery_bot_config=cfg,
            )

        self.assertEqual(process_calls["count"], 0)
        normal_fills = [row for row in result.fill_log if str(row.get("purpose") or "") == "CYCLE_3_SHORT_REDUCE"]
        self.assertFalse(normal_fills)
        recovery_purposes = {
            str(row.get("purpose") or "")
            for row in result.fill_log
            if str(row.get("purpose") or "").startswith("RECOVERY_")
        }
        self.assertTrue(recovery_purposes)
        self.assertEqual(recovery_purposes, {RECOVERY_PAIR_REDUCE_LONG_PURPOSE, RECOVERY_PAIR_REDUCE_SHORT_PURPOSE})


if __name__ == "__main__":
    unittest.main()

