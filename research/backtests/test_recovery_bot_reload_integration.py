from __future__ import annotations

import unittest
from unittest import mock

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_NEUTRALIZE_LONG_PURPOSE,
    RECOVERY_RELOAD_LONG_PURPOSE,
    RECOVERY_RELOAD_SHORT_PURPOSE,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState


class RecoveryBotReloadIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _config(self, *, wait_candles: int = 1) -> RecoveryBotConfig:
        return RecoveryBotConfig(
            enabled=True,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=wait_candles,
            reload_long_notional_usdt=100.0,
            reload_short_notional_usdt=50.0,
            reload_slippage_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
        )

    def _sim(
        self,
        *,
        state: RecoveryState = RecoveryState.READY_TO_CLOSE,
        wait_candles: int = 1,
    ) -> HedgeBotOriginalSimulator:
        cfg = self._config(wait_candles=wait_candles)
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=95.0)
        sim.book.long_qty = 20.0
        sim.book.short_qty = 20.0
        sim.book.long_avg = 100.0
        sim.book.short_avg = 90.0
        sim.book.fee_rate = 0.00055
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="reload_integration_setup", price=95.0)

        tracker = RecoveryBotTracker(config=cfg)
        tracker.state = state
        tracker.loss_budget_usdt = 1.0
        tracker.loss_budget_used_usdt = 0.0
        if state == RecoveryState.MINIMUM_PAIR_REACHED:
            tracker.minimum_pair_reached = True
        sim.recovery_bot_tracker = tracker
        sim.recovery_bot_config = cfg
        sim.stuck_recovery_reload_tracker = None
        self._sims.append(sim)
        return sim

    def test_ready_to_close_to_waiting_to_reload_to_neutralizing(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 94.0, "high": 94.0, "low": 94.0, "close": 94.0},
        ]
        sim = self._sim(state=RecoveryState.READY_TO_CLOSE, wait_candles=1)
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
                max_candles=3,
                recovery_bot_config=self._config(wait_candles=1),
            )

        self.assertEqual(process_calls["count"], 0)
        reload_rows = [
            row for row in result.fill_log if str(row.get("purpose") or "").startswith("RECOVERY_RELOAD_")
        ]
        self.assertEqual(len(reload_rows), 2)
        self.assertTrue(all(int(row.get("candle_index") or -1) == 2 for row in reload_rows))

        neutralize_rows = [
            row for row in result.fill_log if str(row.get("purpose") or "") == RECOVERY_NEUTRALIZE_LONG_PURPOSE
        ]
        self.assertEqual(len(neutralize_rows), 1)
        self.assertEqual(int(neutralize_rows[0].get("candle_index") or -1), 3)
        self.assertEqual(
            {str(row.get("purpose") or "") for row in reload_rows},
            {RECOVERY_RELOAD_LONG_PURPOSE, RECOVERY_RELOAD_SHORT_PURPOSE},
        )

    def test_reload_does_not_happen_in_same_candle_as_waiting_transition(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        ]
        sim = self._sim(state=RecoveryState.READY_TO_CLOSE, wait_candles=1)

        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=2,
                recovery_bot_config=self._config(wait_candles=1),
            )

        reload_rows = [
            row for row in result.fill_log if str(row.get("purpose") or "").startswith("RECOVERY_RELOAD_")
        ]
        self.assertEqual(len(reload_rows), 2)
        self.assertTrue(all(int(row.get("candle_index") or -1) == 2 for row in reload_rows))

    def test_baseline_identity_disabled_and_none_remain_unchanged(self) -> None:
        candles = [
            {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0},
            {"open": 100.0, "high": 100.5, "low": 99.5, "close": 100.1},
            {"open": 100.1, "high": 100.3, "low": 99.8, "close": 99.9},
            {"open": 99.9, "high": 100.2, "low": 99.7, "close": 100.0},
        ]
        baseline = run_historical_backtest("BTCUSDT", "long", candles, max_candles=3, recovery_bot_config=None)
        disabled = run_historical_backtest(
            "BTCUSDT",
            "long",
            candles,
            max_candles=3,
            recovery_bot_config=RecoveryBotConfig(enabled=False),
        )
        self.assertEqual(baseline.final_status, disabled.final_status)
        self.assertEqual(baseline.realized_pnl, disabled.realized_pnl)
        self.assertEqual(len(baseline.fill_log), len(disabled.fill_log))
        for left, right in zip(baseline.fill_log, disabled.fill_log):
            for key in (
                "purpose",
                "side",
                "qty",
                "fill_price",
                "closed_pnl",
                "long_qty_after",
                "short_qty_after",
            ):
                self.assertEqual(left.get(key), right.get(key))


if __name__ == "__main__":
    unittest.main()
