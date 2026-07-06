from __future__ import annotations

from decimal import Decimal
import unittest

from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_NEUTRALIZE_LONG_PURPOSE,
    _compute_neutralization_reduce_qty,
    _estimate_recovery_loss_usdt,
    maybe_execute_neutralization_step,
)
from research.backtests.recovery_bot.state import (
    RecoveryBotTracker,
    RecoveryState,
    recovery_trace_entries,
)
from research.backtests.simulated_order_book import SyntheticCandle


class RecoveryBotNeutralizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _sim(
        self,
        *,
        close: float = 90.0,
        long_qty: float = 120.0,
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
        sim._refresh_snapshot_from_book(source="test_setup", price=close)
        self._sims.append(sim)
        return sim

    def _set_candle(self, sim: HedgeBotOriginalSimulator, *, close: float, index: int = 1) -> None:
        sim.candle = SyntheticCandle(symbol=sim.symbol, close=float(close))
        sim.candle_index = int(index)
        sim._refresh_snapshot_from_book(source="test_candle", price=close)

    def _tracker(
        self,
        *,
        config: RecoveryBotConfig | None = None,
        anchor_price: float = 90.0,
        fixed_step_qty: float = 10.0,
        loss_budget_usdt: float = 1_000.0,
        loss_budget_used_usdt: float = 0.0,
    ) -> RecoveryBotTracker:
        cfg = config or RecoveryBotConfig(enabled=True)
        tracker = RecoveryBotTracker(config=cfg)
        tracker.state = RecoveryState.NEUTRALIZING
        tracker.neutralization_anchor_price = float(anchor_price)
        tracker.neutralization_fixed_step_qty = float(fixed_step_qty)
        tracker.loss_budget_usdt = float(loss_budget_usdt)
        tracker.loss_budget_used_usdt = float(loss_budget_used_usdt)
        return tracker

    def test_no_step_below_price_distance(self) -> None:
        sim = self._sim(close=89.5)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_step_price_drop_pct=1.0,
                neutralize_reduce_mode="fixed_steps",
            )
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.5, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(sim.book.long_qty, 120.0)
        self.assertEqual(tracker.neutralization_steps_done, 0)

    def test_step_exactly_at_one_percent_drop(self) -> None:
        sim = self._sim(close=89.1)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_step_price_drop_pct=1.0,
                neutralize_reduce_mode="fixed_steps",
            )
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].purpose, RECOVERY_NEUTRALIZE_LONG_PURPOSE)
        self.assertAlmostEqual(sim.book.long_qty, 110.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 70.0, places=6)

    def test_max_one_step_per_candle_and_anchor_updates(self) -> None:
        sim = self._sim(close=89.1)
        tracker = self._tracker()
        first = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(first), 1)
        self.assertAlmostEqual(tracker.neutralization_anchor_price or 0.0, 89.1, places=6)
        second = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(second, [])
        self.assertEqual(tracker.neutralization_steps_done, 1)

    def test_fixed_steps_uses_constant_absolute_qty(self) -> None:
        sim = self._sim(close=89.1)
        tracker = self._tracker(
            config=RecoveryBotConfig(enabled=True, neutralize_reduce_mode="fixed_steps")
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertAlmostEqual(fills[0].exec_qty, 10.0, places=6)

    def test_fixed_qty_uses_configured_qty(self) -> None:
        sim = self._sim(close=89.1)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_reduce_mode="fixed_qty",
                neutralize_reduce_qty=7.0,
            ),
            fixed_step_qty=10.0,
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertAlmostEqual(fills[0].exec_qty, 7.0, places=6)

    def test_percent_uses_current_net_long_percent(self) -> None:
        sim = self._sim(close=89.1)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_reduce_mode="percent",
                neutralize_reduce_pct=50.0,
            ),
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertAlmostEqual(fills[0].exec_qty, 25.0, places=6)

    def test_step_never_exceeds_net_long(self) -> None:
        sim = self._sim(close=89.1, long_qty=75.0, short_qty=70.0)
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_reduce_mode="fixed_qty",
                neutralize_reduce_qty=20.0,
            ),
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertAlmostEqual(fills[0].exec_qty, 5.0, places=6)
        self.assertGreaterEqual(sim.book.long_qty, sim.book.short_qty)

    def test_last_step_neutralizes_exactly_and_transitions(self) -> None:
        sim = self._sim(close=89.1, long_qty=79.9, short_qty=70.0)
        sim.runtime_state.instrument_rules[sim.symbol]["qty_step"] = Decimal("0.1")
        tracker = self._tracker(
            config=RecoveryBotConfig(enabled=True, neutralize_reduce_mode="fixed_steps"),
            fixed_step_qty=10.0,
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(sim.book.long_qty, 70.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 70.0, places=6)
        self.assertEqual(tracker.state, RecoveryState.PAIR_REDUCING)
        self.assertAlmostEqual(tracker.pair_anchor_price or 0.0, 89.1, places=6)

    def test_qty_step_rounding_never_overshoots_short(self) -> None:
        sim = self._sim(close=89.1, long_qty=70.4, short_qty=70.0)
        sim.runtime_state.instrument_rules[sim.symbol]["qty_step"] = Decimal("0.3")
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_reduce_mode="fixed_qty",
                neutralize_reduce_qty=1.0,
            ),
            fixed_step_qty=1.0,
        )
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertGreaterEqual(sim.book.long_qty, sim.book.short_qty)
        self.assertAlmostEqual(fills[0].exec_qty, 0.3, places=6)

    def test_min_order_qty_blocks_untradeable_step(self) -> None:
        sim = self._sim(close=89.1, long_qty=75.0, short_qty=70.0)
        sim.runtime_state.instrument_rules[sim.symbol]["min_order_qty"] = Decimal("10")
        tracker = self._tracker(
            config=RecoveryBotConfig(
                enabled=True,
                neutralize_reduce_mode="fixed_qty",
                neutralize_reduce_qty=5.0,
            ),
            fixed_step_qty=5.0,
        )
        before_orders = len(sim.order_log)
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(len(sim.order_log), before_orders)
        self.assertEqual(tracker.blocked_reason, "neutralization_untradeable_residual")

    def test_full_step_within_budget_keeps_original_quantity(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=1_000.0, loss_budget_used_usdt=0.0)
        planned_qty = _compute_neutralization_reduce_qty(sim, tracker)
        self.assertGreater(planned_qty, 0.0)
        expected_loss = _estimate_recovery_loss_usdt(sim, tracker, qty=float(planned_qty), current_price=89.1)
        self.assertGreater(expected_loss, 0.0)
        self.assertLess(expected_loss, float(tracker.loss_budget_usdt or 0.0))

        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0].exec_qty, planned_qty, places=6)

    def test_step_is_adjusted_to_fit_remaining_budget(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=1_000.0, loss_budget_used_usdt=0.0)
        planned_qty = _compute_neutralization_reduce_qty(sim, tracker)
        self.assertGreater(planned_qty, 0.0)
        full_loss = _estimate_recovery_loss_usdt(sim, tracker, qty=float(planned_qty), current_price=89.1)
        self.assertGreater(full_loss, 0.0)

        remaining_budget = float(full_loss) * 0.5
        tracker.loss_budget_usdt = remaining_budget

        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        exec_qty = float(fills[0].exec_qty)
        self.assertLess(exec_qty, planned_qty)

        rules = sim.runtime_state.instrument_rules[sim.symbol]
        qty_step = float(rules.get("qty_step") or 0.001)
        units = exec_qty / qty_step
        self.assertAlmostEqual(units, round(units), places=6)

        loss_after = _estimate_recovery_loss_usdt(sim, tracker, qty=exec_qty, current_price=89.1)
        self.assertLessEqual(loss_after, remaining_budget + 1e-8)

    def test_rest_budget_too_small_blocks_with_budget_reason(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=1e-6, loss_budget_used_usdt=0.0)
        before_orders = len(sim.order_log)
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(fills, [])
        self.assertEqual(len(sim.order_log), before_orders)
        self.assertEqual(tracker.blocked_reason, "neutralization_blocked_by_loss_budget")

    def test_waiting_candle_does_not_clear_budget_blocker(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=1e-6, loss_budget_used_usdt=0.0)

        fills_first = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(fills_first, [])
        self.assertEqual(tracker.blocked_reason, "neutralization_blocked_by_loss_budget")

        self._set_candle(sim, close=89.5, index=2)
        fills_second = maybe_execute_neutralization_step(sim, tracker, current_price=89.5, candle_index=2)
        self.assertEqual(fills_second, [])
        self.assertEqual(tracker.blocked_reason, "neutralization_blocked_by_loss_budget")

    def test_later_low_price_does_not_overwrite_budget_block_with_untradeable(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=1e-6, loss_budget_used_usdt=0.0)

        fills_first = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(fills_first, [])
        self.assertEqual(tracker.blocked_reason, "neutralization_blocked_by_loss_budget")

        sim.runtime_state.instrument_rules[sim.symbol]["min_notional"] = Decimal("1000")
        self._set_candle(sim, close=80.0, index=2)
        fills_second = maybe_execute_neutralization_step(sim, tracker, current_price=80.0, candle_index=2)
        self.assertEqual(fills_second, [])
        self.assertEqual(tracker.blocked_reason, "neutralization_blocked_by_loss_budget")

    def test_budget_adjustment_traces_diagnostics_fields(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker(loss_budget_usdt=1_000.0, loss_budget_used_usdt=0.0)
        planned_qty = _compute_neutralization_reduce_qty(sim, tracker)
        full_loss = _estimate_recovery_loss_usdt(sim, tracker, qty=float(planned_qty), current_price=89.1)
        remaining_budget = float(full_loss) * 0.5
        tracker.loss_budget_usdt = remaining_budget

        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)

        trace = recovery_trace_entries(tracker)
        diag_entries = [entry for entry in trace if str(entry.get("action") or "") == "NEUTRALIZATION_FILLED"]
        self.assertTrue(diag_entries)
        entry = diag_entries[-1]

        self.assertIn("planned_reduce_qty", entry)
        self.assertIn("adjusted_reduce_qty", entry)
        self.assertIn("expected_loss_before_adjustment", entry)
        self.assertIn("expected_loss_after_adjustment", entry)
        self.assertIn("remaining_loss_budget_usdt", entry)

        self.assertGreater(float(entry["planned_reduce_qty"]), float(entry["adjusted_reduce_qty"]))
        self.assertGreaterEqual(
            float(entry["expected_loss_before_adjustment"]),
            float(entry["expected_loss_after_adjustment"]),
        )
        self.assertAlmostEqual(float(entry["remaining_loss_budget_usdt"]), remaining_budget, places=6)

    def test_successful_step_creates_order_and_fill_and_updates_budget(self) -> None:
        sim = self._sim(close=89.1, long_avg=100.0)
        tracker = self._tracker()
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0].purpose, RECOVERY_NEUTRALIZE_LONG_PURPOSE)
        self.assertAlmostEqual(sim.book.long_qty, 110.0, places=6)
        self.assertAlmostEqual(sim.book.short_qty, 70.0, places=6)
        self.assertGreater(tracker.loss_budget_used_usdt, 0.0)
        purposes = {str(row.get("purpose") or "") for row in sim.order_log}
        self.assertIn(RECOVERY_NEUTRALIZE_LONG_PURPOSE, purposes)

    def test_positive_fill_does_not_reduce_used_budget(self) -> None:
        sim = self._sim(close=89.1, long_avg=80.0)
        tracker = self._tracker(loss_budget_used_usdt=1.5)
        fills = maybe_execute_neutralization_step(sim, tracker, current_price=89.1, candle_index=1)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(tracker.loss_budget_used_usdt, 1.5, places=6)


if __name__ == "__main__":
    unittest.main()

