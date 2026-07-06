from __future__ import annotations

from decimal import Decimal
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.models import FillEvent

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_FINAL_EXIT_LONG_PURPOSE,
    RECOVERY_FINAL_EXIT_SHORT_PURPOSE,
    maybe_execute_recovery_final_exit,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState
from research.backtests.simulated_order_book import SyntheticCandle


class RecoveryBotFinalExitTests(unittest.TestCase):
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
    ) -> HedgeBotOriginalSimulator:
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=close)
        sim.book.long_qty = float(long_qty)
        sim.book.short_qty = float(short_qty)
        sim.book.long_avg = float(long_avg)
        sim.book.short_avg = float(short_avg)
        sim.book.fee_rate = 0.00055
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="final_exit_setup", price=close)
        self._sims.append(sim)
        return sim

    def _tracker(self, *, loss_budget_usdt: float = 10.0, used_budget: float = 0.0) -> RecoveryBotTracker:
        tracker = RecoveryBotTracker(config=RecoveryBotConfig(enabled=True))
        tracker.state = RecoveryState.READY_TO_CLOSE
        tracker.loss_budget_usdt = float(loss_budget_usdt)
        tracker.loss_budget_used_usdt = float(used_budget)
        return tracker

    def test_no_exit_outside_ready_to_close(self) -> None:
        sim = self._sim()
        tracker = self._tracker()
        tracker.state = RecoveryState.WAITING_FOR_RELOAD
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertFalse(tracker.final_exit_attempted)

    def test_already_flat_moves_directly_to_closed(self) -> None:
        sim = self._sim(long_qty=0.0, short_qty=0.0, long_avg=0.0, short_avg=0.0)
        tracker = self._tracker()
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.state, RecoveryState.CLOSED)
        self.assertEqual(tracker.final_exit_reason, "already_flat")
        self.assertEqual(tracker.remaining_long_qty, 0.0)
        self.assertEqual(tracker.remaining_short_qty, 0.0)

    def test_full_exit_within_budget_closes_both_legs(self) -> None:
        sim = self._sim()
        tracker = self._tracker(loss_budget_usdt=10.0, used_budget=1.0)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertEqual(tracker.state, RecoveryState.CLOSED)
        self.assertEqual(tracker.final_exit_reason, "recovery_full_exit_within_budget")
        self.assertAlmostEqual(sim.book.long_qty, 0.0, places=12)
        self.assertAlmostEqual(sim.book.short_qty, 0.0, places=12)
        self.assertFalse(sim.book.active_orders())

    def test_exit_purposes_reduce_only_market_and_full_qty(self) -> None:
        sim = self._sim(long_qty=21.0, short_qty=19.5)
        tracker = self._tracker(loss_budget_usdt=20.0)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertEqual({fill.purpose for fill in fills}, {RECOVERY_FINAL_EXIT_LONG_PURPOSE, RECOVERY_FINAL_EXIT_SHORT_PURPOSE})
        self.assertTrue(all(fill.reduce_only for fill in fills))
        self.assertTrue(all(fill.order_type == "Market" for fill in fills))
        fill_qty_by_purpose = {fill.purpose: fill.exec_qty for fill in fills}
        self.assertAlmostEqual(fill_qty_by_purpose[RECOVERY_FINAL_EXIT_LONG_PURPOSE], 21.0, places=6)
        self.assertAlmostEqual(fill_qty_by_purpose[RECOVERY_FINAL_EXIT_SHORT_PURPOSE], 19.5, places=6)

        submitted_rows = [
            row for row in sim.order_log if str(row.get("event_type") or "") == "submitted"
        ]
        self.assertEqual(len(submitted_rows), 2)
        self.assertTrue(all(bool(row.get("reduce_only")) for row in submitted_rows))

    def test_common_prevalidation_blocks_both_orders(self) -> None:
        sim = self._sim(long_qty=20.0, short_qty=0.0005)
        tracker = self._tracker(loss_budget_usdt=20.0)
        before_orders = len(sim.order_log)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(len(sim.order_log), before_orders)
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "final_exit_untradeable_residual")

    def test_budget_block_moves_to_waiting_without_orders(self) -> None:
        sim = self._sim(long_avg=100.0, short_avg=70.0)
        tracker = self._tracker(loss_budget_usdt=3.0, used_budget=2.9)
        before_orders = len(sim.order_log)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(len(sim.order_log), before_orders)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_RELOAD)
        self.assertEqual(tracker.blocked_reason, "final_exit_blocked_by_loss_budget")
        self.assertEqual(tracker.final_exit_reason, "final_exit_outside_loss_budget")

    def test_negative_combined_pnl_increases_used_budget(self) -> None:
        sim = self._sim(long_qty=1.0, short_qty=1.0, long_avg=100.0, short_avg=99.8)
        tracker = self._tracker(loss_budget_usdt=20.0, used_budget=1.0)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertGreater(tracker.loss_budget_used_usdt, 1.0)
        self.assertLess(tracker.final_exit_combined_pnl, 0.0)

    def test_positive_combined_pnl_does_not_reduce_budget_used(self) -> None:
        sim = self._sim(long_avg=90.0, short_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=20.0, used_budget=1.5)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(tracker.loss_budget_used_usdt, 1.5, places=6)
        self.assertGreater(tracker.recovery_realized_pnl, 0.0)

    def test_no_direct_book_qty_manipulation(self) -> None:
        sim = self._sim()
        tracker = self._tracker(loss_budget_usdt=20.0)
        before_long = sim.book.long_qty
        before_short = sim.book.short_qty
        with mock.patch(
            "research.backtests.recovery_bot.engine.fill_order_at_candle_close",
            side_effect=[
                FillEvent(
                    exchange_order_id="ex1",
                    client_order_id="c1",
                    side="long",
                    purpose=RECOVERY_FINAL_EXIT_LONG_PURPOSE,
                    exec_qty=20.0,
                    exec_price=95.0,
                    order_type="Market",
                    reduce_only=True,
                    status="FILLED",
                    metadata={"closed_pnl": 0.0},
                ),
                FillEvent(
                    exchange_order_id="ex2",
                    client_order_id="c2",
                    side="short",
                    purpose=RECOVERY_FINAL_EXIT_SHORT_PURPOSE,
                    exec_qty=20.0,
                    exec_price=95.0,
                    order_type="Market",
                    reduce_only=True,
                    status="FILLED",
                    metadata={"closed_pnl": 0.0},
                ),
            ],
        ):
            fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(sim.book.long_qty, before_long, places=6)
        self.assertAlmostEqual(sim.book.short_qty, before_short, places=6)
        self.assertEqual(tracker.state, RecoveryState.FAILED)

    def test_partial_fill_failure_marks_failed(self) -> None:
        sim = self._sim()
        tracker = self._tracker(loss_budget_usdt=20.0)
        with mock.patch(
            "research.backtests.recovery_bot.engine.fill_order_at_candle_close",
            side_effect=[
                FillEvent(
                    exchange_order_id="ex1",
                    client_order_id="c1",
                    side="long",
                    purpose=RECOVERY_FINAL_EXIT_LONG_PURPOSE,
                    exec_qty=20.0,
                    exec_price=95.0,
                    order_type="Market",
                    reduce_only=True,
                    status="FILLED",
                    metadata={"closed_pnl": 0.0},
                ),
                RuntimeError("short fill failed"),
            ],
        ):
            fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "final_exit_atomicity_failed")
        self.assertEqual(tracker.final_exit_reason, "partial_final_exit")

    def test_untradeable_residual_never_sets_closed(self) -> None:
        sim = self._sim(long_qty=20.05, short_qty=20.0)
        sim.runtime_state.instrument_rules[sim.symbol]["qty_step"] = Decimal("0.1")
        tracker = self._tracker(loss_budget_usdt=20.0)
        fills = maybe_execute_recovery_final_exit(sim, tracker, current_price=95.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertNotEqual(tracker.state, RecoveryState.CLOSED)
        self.assertEqual(tracker.blocked_reason, "final_exit_untradeable_residual")


if __name__ == "__main__":
    unittest.main()
