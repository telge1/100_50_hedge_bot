from __future__ import annotations

from decimal import Decimal
import unittest
from unittest import mock

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_PAIR_REDUCE_LONG_PURPOSE,
    RECOVERY_PAIR_REDUCE_SHORT_PURPOSE,
    maybe_advance_minimum_pair_state,
    maybe_execute_pair_reduction_step,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState
from research.backtests.simulated_order_book import SyntheticCandle


class RecoveryBotPairReductionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _sim(
        self,
        *,
        close: float = 101.0,
        long_qty: float = 70.0,
        short_qty: float = 70.0,
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
        sim._refresh_snapshot_from_book(source="pair_setup", price=close)
        self._sims.append(sim)
        return sim

    def _tracker(self, *, config: RecoveryBotConfig | None = None, anchor_price: float = 100.0) -> RecoveryBotTracker:
        cfg = config or RecoveryBotConfig(
            enabled=True,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=10.0,
        )
        tracker = RecoveryBotTracker(config=cfg)
        tracker.state = RecoveryState.PAIR_REDUCING
        tracker.pair_anchor_price = float(anchor_price)
        tracker.loss_budget_usdt = 1_000.0
        return tracker

    def _set_candle(self, sim: HedgeBotOriginalSimulator, *, close: float, index: int = 1) -> None:
        sim.candle = SyntheticCandle(symbol=sim.symbol, close=float(close))
        sim.candle_index = index
        sim._refresh_snapshot_from_book(source="pair_test_candle", price=close)

    def test_no_step_below_move_threshold(self) -> None:
        sim = self._sim(close=100.5)
        tracker = self._tracker()
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=100.5, candle_index=1)
        self.assertEqual(fills, [])

    def test_up_move_triggers_pair_step(self) -> None:
        sim = self._sim(close=101.0)
        tracker = self._tracker()
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertEqual({fills[0].purpose, fills[1].purpose}, {RECOVERY_PAIR_REDUCE_LONG_PURPOSE, RECOVERY_PAIR_REDUCE_SHORT_PURPOSE})
        self.assertAlmostEqual(sim.book.long_qty, 60.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 60.0, places=6)

    def test_down_move_triggers_pair_step(self) -> None:
        sim = self._sim(close=99.0)
        tracker = self._tracker()
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=99.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(sim.book.long_qty, 60.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 60.0, places=6)

    def test_disabled_up_or_down_move_blocks_step(self) -> None:
        sim = self._sim(close=101.0)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                pair_reduce_move_pct=1.0,
                pair_reduce_on_up_move=False,
                pair_reduce_on_down_move=True,
                pair_reduce_mode="fixed_qty",
                pair_reduce_qty=10.0,
            )
        )
        self.assertEqual(
            maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1),
            [],
        )

        sim2 = self._sim(close=99.0)
        tracker2 = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                pair_reduce_move_pct=1.0,
                pair_reduce_on_up_move=True,
                pair_reduce_on_down_move=False,
                pair_reduce_mode="fixed_qty",
                pair_reduce_qty=10.0,
            )
        )
        self.assertEqual(
            maybe_execute_pair_reduction_step(sim2, tracker2, current_price=99.0, candle_index=1),
            [],
        )

    def test_anchor_updates_only_after_successful_dual_fill(self) -> None:
        sim = self._sim(close=101.0)
        tracker = self._tracker(anchor_price=100.0)
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(tracker.pair_anchor_price or 0.0, 101.0, places=6)

        sim2 = self._sim(close=100.5)
        tracker2 = self._tracker(anchor_price=100.0)
        fills2 = maybe_execute_pair_reduction_step(sim2, tracker2, current_price=100.5, candle_index=1)
        self.assertEqual(fills2, [])
        self.assertAlmostEqual(tracker2.pair_anchor_price or 0.0, 100.0, places=6)

    def test_fixed_qty_and_percent_use_same_common_qty(self) -> None:
        sim = self._sim(close=101.0)
        tracker = self._tracker()
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertAlmostEqual(fills[0].exec_qty, fills[1].exec_qty, places=6)
        self.assertAlmostEqual(fills[0].exec_qty, 10.0, places=6)

        sim2 = self._sim(close=101.0)
        tracker2 = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                pair_reduce_move_pct=1.0,
                pair_reduce_mode="percent",
                pair_reduce_pct=10.0,
            )
        )
        fills2 = maybe_execute_pair_reduction_step(sim2, tracker2, current_price=101.0, candle_index=1)
        self.assertAlmostEqual(fills2[0].exec_qty, fills2[1].exec_qty, places=6)
        self.assertAlmostEqual(fills2[0].exec_qty, 7.0, places=6)

    def test_common_rounding_and_neutrality_preserved(self) -> None:
        sim = self._sim(close=101.0, long_qty=70.4, short_qty=70.4)
        sim.runtime_state.instrument_rules[sim.symbol]["qty_step"] = Decimal("0.3")
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                pair_reduce_move_pct=1.0,
                pair_reduce_mode="fixed_qty",
                pair_reduce_qty=1.0,
            )
        )
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(fills[0].exec_qty, fills[1].exec_qty, places=6)
        self.assertAlmostEqual(sim.book.long_qty, sim.book.short_qty, places=6)

    def test_budget_block_and_anchor_unchanged(self) -> None:
        sim = self._sim(close=90.0, long_avg=100.0, short_avg=80.0)
        tracker = self._tracker(anchor_price=100.0)
        tracker.loss_budget_usdt = 1.0
        before_orders = len(sim.order_log)
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=90.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(len(sim.order_log), before_orders)
        self.assertEqual(tracker.blocked_reason, "pair_reduction_blocked_by_loss_budget")
        self.assertAlmostEqual(tracker.pair_anchor_price or 0.0, 100.0, places=6)

    def test_negative_and_positive_combined_pnl_budget_accounting(self) -> None:
        sim = self._sim(close=90.0, long_avg=100.0, short_avg=80.0)
        tracker = self._tracker()
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=90.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertGreater(tracker.loss_budget_used_usdt, 0.0)

        sim2 = self._sim(close=101.0, long_avg=90.0, short_avg=110.0)
        tracker2 = self._tracker()
        tracker2.loss_budget_used_usdt = 1.5
        fills2 = maybe_execute_pair_reduction_step(sim2, tracker2, current_price=101.0, candle_index=1)
        self.assertEqual(len(fills2), 2)
        self.assertAlmostEqual(tracker2.loss_budget_used_usdt, 1.5, places=6)

    def test_minimum_qty_notional_and_state_progression(self) -> None:
        sim = self._sim(close=101.0, long_qty=25.0, short_qty=25.0, long_avg=100.0, short_avg=100.0)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                pair_reduce_move_pct=1.0,
                pair_reduce_mode="fixed_qty",
                pair_reduce_qty=10.0,
                minimum_pair_qty=20.0,
                minimum_pair_notional_usdt=0.0,
            )
        )
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertEqual(len(fills), 2)
        self.assertAlmostEqual(sim.book.long_qty, 20.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 20.0, places=6)
        self.assertEqual(tracker.state, RecoveryState.MINIMUM_PAIR_REACHED)
        self.assertTrue(tracker.minimum_pair_reached)

        advanced = maybe_advance_minimum_pair_state(sim, tracker, current_price=101.0)
        self.assertTrue(advanced)
        self.assertIn(tracker.state, {RecoveryState.READY_TO_CLOSE, RecoveryState.WAITING_FOR_RELOAD})

    def test_pair_not_neutral_blocks(self) -> None:
        sim = self._sim(close=101.0, long_qty=70.1, short_qty=70.0)
        tracker = self._tracker()
        fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(tracker.blocked_reason, "pair_not_neutral")

    def test_atomicity_failure_marks_failed(self) -> None:
        sim = self._sim(close=101.0)
        tracker = self._tracker()
        with mock.patch(
            "research.backtests.recovery_bot.engine.fill_order_at_candle_close",
            side_effect=[
                mock.Mock(
                    client_order_id="first",
                    exec_price=101.0,
                    exec_qty=10.0,
                    purpose=RECOVERY_PAIR_REDUCE_LONG_PURPOSE,
                    metadata={"closed_pnl": 0.0},
                ),
                RuntimeError("fill failed"),
            ],
        ):
            fills = maybe_execute_pair_reduction_step(sim, tracker, current_price=101.0, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "pair_reduction_atomicity_failed")


if __name__ == "__main__":
    unittest.main()

