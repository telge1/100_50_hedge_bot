from __future__ import annotations

import unittest
from unittest import mock

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_FINAL_EXIT_LONG_PURPOSE,
    RECOVERY_FINAL_EXIT_SHORT_PURPOSE,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState


class RecoveryBotFinalExitIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _sim(
        self,
        *,
        close: float = 95.0,
        long_qty: float = 20.0,
        short_qty: float = 20.0,
        long_avg: float = 100.0,
        short_avg: float = 100.0,
        state: RecoveryState = RecoveryState.READY_TO_CLOSE,
        loss_budget_usdt: float = 10.0,
    ) -> HedgeBotOriginalSimulator:
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=close)
        sim.book.long_qty = float(long_qty)
        sim.book.short_qty = float(short_qty)
        sim.book.long_avg = float(long_avg)
        sim.book.short_avg = float(short_avg)
        sim.book.fee_rate = 0.00055
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="final_exit_integration_setup", price=close)
        tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True))
        tracker.state = state
        tracker.loss_budget_usdt = float(loss_budget_usdt)
        if state == RecoveryState.MINIMUM_PAIR_REACHED:
            tracker.minimum_pair_reached = True
        sim.recovery_bot_tracker = tracker
        sim.recovery_bot_config = RecoveryBotConfig(enabled=True)
        sim.stuck_recovery_reload_tracker = None
        self._sims.append(sim)
        return sim

    def test_historical_backtest_closes_trade_and_keeps_strategy_frozen(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 94.0, "high": 94.0, "low": 94.0, "close": 94.0},
        ]
        sim = self._sim()
        original_process = sim.process_candle
        process_calls = {"count": 0}

        def _wrapped_process(*args, **kwargs):
            process_calls["count"] += 1
            return original_process(*args, **kwargs)

        sim.process_candle = _wrapped_process  # type: ignore[assignment]

        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=2,
                recovery_bot_config=RecoveryBotConfig(enabled=True),
            )

        self.assertEqual(process_calls["count"], 0)
        self.assertEqual(result.final_status, "closed")
        self.assertEqual(result.exit_reason, "flat_no_active_orders")
        self.assertEqual(result.candles_processed, 1)
        purposes = {str(row.get("purpose") or "") for row in result.fill_log if str(row.get("purpose") or "").startswith("RECOVERY_")}
        self.assertEqual(purposes, {RECOVERY_FINAL_EXIT_LONG_PURPOSE, RECOVERY_FINAL_EXIT_SHORT_PURPOSE})

    def test_waiting_for_reload_produces_no_orders(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        ]
        sim = self._sim(state=RecoveryState.WAITING_FOR_RELOAD)
        original_process = sim.process_candle
        process_calls = {"count": 0}

        def _wrapped_process(*args, **kwargs):
            process_calls["count"] += 1
            return original_process(*args, **kwargs)

        sim.process_candle = _wrapped_process  # type: ignore[assignment]

        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=1,
                recovery_bot_config=RecoveryBotConfig(enabled=True),
            )

        self.assertEqual(process_calls["count"], 0)
        self.assertFalse([row for row in result.fill_log if str(row.get("purpose") or "").startswith("RECOVERY_FINAL_EXIT_")])
        self.assertEqual(sim.recovery_bot_tracker.state, RecoveryState.WAITING_FOR_RELOAD)

    def test_minimum_pair_to_ready_and_final_exit_not_same_candle(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        ]
        sim = self._sim(state=RecoveryState.MINIMUM_PAIR_REACHED)

        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=2,
                recovery_bot_config=RecoveryBotConfig(enabled=True),
            )

        final_exit_rows = [
            row for row in result.fill_log if str(row.get("purpose") or "").startswith("RECOVERY_FINAL_EXIT_")
        ]
        self.assertEqual(len(final_exit_rows), 2)
        self.assertTrue(all(int(row.get("candle_index") or -1) == 2 for row in final_exit_rows))


if __name__ == "__main__":
    unittest.main()
