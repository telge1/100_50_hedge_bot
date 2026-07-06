from __future__ import annotations

import unittest
from unittest import mock

from fixed_cycle_hedge_bot.models import FillEvent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_RELOAD_LONG_PURPOSE,
    RECOVERY_RELOAD_SHORT_PURPOSE,
    maybe_execute_recovery_reload,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState


class RecoveryBotReloadTests(unittest.TestCase):
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
        short_avg: float = 90.0,
    ) -> HedgeBotOriginalSimulator:
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=close)
        sim.book.long_qty = float(long_qty)
        sim.book.short_qty = float(short_qty)
        sim.book.long_avg = float(long_avg)
        sim.book.short_avg = float(short_avg)
        sim.book.fee_rate = 0.00055
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="reload_setup", price=close)
        self._sims.append(sim)
        return sim

    def _tracker(
        self,
        *,
        reload_enabled: bool = True,
        wait_candles: int = 0,
        max_reloads: int = 1,
        long_notional: float = 100.0,
        short_notional: float = 50.0,
        max_total_notional: float | None = None,
        slippage_pct: float = 0.0,
    ) -> RecoveryBotTracker:
        tracker = RecoveryBotTracker(
            config=RecoveryBotConfig(
                enabled=True,
                reload_enabled=reload_enabled,
                max_reloads_per_trade=max_reloads,
                reload_wait_candles=wait_candles,
                reload_long_notional_usdt=long_notional,
                reload_short_notional_usdt=short_notional,
                reload_max_total_notional_usdt=max_total_notional,
                reload_slippage_pct=slippage_pct,
            )
        )
        tracker.state = RecoveryState.WAITING_FOR_RELOAD
        tracker.waiting_for_reload_since_candle_index = 0
        tracker.loss_budget_usdt = 3.0
        tracker.loss_budget_used_usdt = 1.0
        return tracker

    def test_reload_disabled_keeps_waiting(self) -> None:
        sim = self._sim()
        tracker = self._tracker(reload_enabled=False)
        fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_RELOAD)

    def test_wait_time_not_reached(self) -> None:
        sim = self._sim()
        tracker = self._tracker(wait_candles=3)
        fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=2)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.reload_count, 0)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_RELOAD)

    def test_successful_atomic_reload(self) -> None:
        sim = self._sim()
        tracker = self._tracker(slippage_pct=1.0)
        before_used = tracker.loss_budget_used_usdt
        fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertEqual({fill.purpose for fill in fills}, {RECOVERY_RELOAD_LONG_PURPOSE, RECOVERY_RELOAD_SHORT_PURPOSE})
        self.assertGreater(sim.book.long_qty, 20.0)
        self.assertGreater(sim.book.short_qty, 20.0)
        self.assertLess(sim.book.long_avg, 100.0)
        self.assertGreater(sim.book.long_avg, 95.0)
        self.assertGreater(sim.book.short_avg, 90.0)
        self.assertLess(sim.book.short_avg, 95.0)
        self.assertEqual(tracker.reload_count, 1)
        self.assertTrue(tracker.reload_attempted)
        self.assertEqual(tracker.reload_candle_index, 1)
        self.assertEqual(tracker.state, RecoveryState.NEUTRALIZING)
        self.assertEqual(tracker.reload_reason, "recovery_reload_filled")
        self.assertAlmostEqual(tracker.loss_budget_used_usdt, before_used, places=6)

    def test_leg_under_min_order_qty_fails_atomically(self) -> None:
        sim = self._sim()
        tracker = self._tracker()
        with mock.patch(
            "research.backtests.recovery_bot.engine._resolve_reload_qty",
            side_effect=[0.0005, 0.5],
        ):
            fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "recovery_reload_untradeable")
        self.assertAlmostEqual(sim.book.long_qty, 20.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 20.0, places=6)

    def test_leg_under_min_notional_fails_atomically(self) -> None:
        sim = self._sim()
        sim.runtime_state.instrument_rules[sim.symbol]["min_notional"] = 100.0
        tracker = self._tracker()
        with mock.patch(
            "research.backtests.recovery_bot.engine._resolve_reload_qty",
            side_effect=[0.5, 0.5],
        ):
            fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "recovery_reload_untradeable")

    def test_notional_limit_exceeded_blocks(self) -> None:
        sim = self._sim()
        tracker = self._tracker(max_total_notional=120.0)
        fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "recovery_reload_notional_limit_exceeded")

    def test_max_reloads_reached_is_stable(self) -> None:
        sim = self._sim()
        tracker = self._tracker(max_reloads=1)
        tracker.reload_count = 1
        for candle_index in (1, 2, 3):
            fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=candle_index)
            self.assertEqual(fills, [])
            self.assertEqual(tracker.reload_count, 1)
            self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_RELOAD)
            self.assertEqual(tracker.blocked_reason, "max_recovery_reloads_reached")

    def test_idempotent_within_same_candle(self) -> None:
        sim = self._sim()
        tracker = self._tracker()
        first = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        second = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])
        self.assertEqual(tracker.reload_count, 1)

    def test_partial_fill_marks_failed(self) -> None:
        sim = self._sim()
        tracker = self._tracker()
        with mock.patch(
            "research.backtests.recovery_bot.engine.fill_order_at_price",
            side_effect=[
                FillEvent(
                    exchange_order_id="ex1",
                    client_order_id="c1",
                    side="long",
                    purpose=RECOVERY_RELOAD_LONG_PURPOSE,
                    exec_qty=1.0,
                    exec_price=95.0,
                    order_type="Market",
                    reduce_only=False,
                    status="FILLED",
                    metadata={"closed_pnl": 0.0},
                ),
                RuntimeError("short reload failed"),
            ],
        ):
            fills = maybe_execute_recovery_reload(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "recovery_reload_atomicity_failed")
        self.assertEqual(tracker.reload_reason, "partial_reload")


if __name__ == "__main__":
    unittest.main()
