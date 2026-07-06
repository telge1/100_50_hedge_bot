from __future__ import annotations

from decimal import Decimal
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.models import FillEvent

from research.backtests.hedge_bot_original_simulator import (
    HedgeBotOriginalSimulator,
    ProcessCandleResult,
)
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig
from research.backtests.recovery_bot.engine import (
    RECOVERY_FINAL_EXIT_LONG_PURPOSE,
    RECOVERY_FINAL_EXIT_SHORT_PURPOSE,
    RECOVERY_RELOAD_LONG_PURPOSE,
    RECOVERY_RELOAD_SHORT_PURPOSE,
    collect_recovery_invariant_violations,
)
from research.backtests.recovery_bot.state import RecoveryBotTracker, RecoveryState


def _main_phase_name(action: str) -> str | None:
    if action.startswith("NEUTRALIZATION_"):
        return "NEUTRALIZATION"
    if action.startswith("PAIR_REDUCTION_"):
        return "PAIR_REDUCTION"
    if action.startswith("FINAL_EXIT_"):
        return "FINAL_EXIT"
    if action in {"RELOAD_SUBMITTED", "RELOAD_FILLED"}:
        return "RELOAD"
    return None


class RecoveryBotE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self._sims: list[HedgeBotOriginalSimulator] = []

    def tearDown(self) -> None:
        for sim in self._sims:
            sim.close()

    def _build_trigger_fill(self, *, price: float, purpose: str = "CYCLE_3_SHORT_REDUCE") -> FillEvent:
        return FillEvent(
            exchange_order_id="trigger-ex",
            client_order_id="trigger-client",
            side="short",
            purpose=purpose,
            exec_qty=1.0,
            exec_price=float(price),
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            metadata={"closed_pnl": 0.0},
        )

    def _sim(
        self,
        *,
        close: float,
        long_qty: float,
        short_qty: float,
        long_avg: float,
        short_avg: float,
        config: RecoveryBotConfig,
        qty_step: float = 0.001,
        fee_rate: float = 0.00055,
    ) -> HedgeBotOriginalSimulator:
        sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=close)
        sim.book.long_qty = float(long_qty)
        sim.book.short_qty = float(short_qty)
        sim.book.long_avg = float(long_avg)
        sim.book.short_avg = float(short_avg)
        sim.book.fee_rate = float(fee_rate)
        sim.runtime_state.instrument_rules[sim.symbol]["qty_step"] = Decimal(str(qty_step))
        sim.book.sync_runtime_state(sim.runtime_state)
        sim.runtime_state.strategy_state["trade_block_id"] = "tb-e2e"
        sim._refresh_snapshot_from_book(source="recovery_e2e_setup", price=close)
        sim.recovery_bot_tracker = RecoveryBotTracker(config=config)
        sim.recovery_bot_config = config
        sim.stuck_recovery_reload_tracker = None
        self._sims.append(sim)
        return sim

    def _run_with_mocked_sim(
        self,
        *,
        candles: list[dict[str, float]],
        sim: HedgeBotOriginalSimulator,
        trigger_price: float,
        emit_trigger: bool = True,
    ):
        process_calls = {"count": 0}
        trigger_sent = {"done": False}

        def _process_candle(candle, **_kwargs):
            process_calls["count"] += 1
            sim.candle = candle
            fills = []
            if emit_trigger and not trigger_sent["done"]:
                fills = [self._build_trigger_fill(price=trigger_price)]
                trigger_sent["done"] = True
            return ProcessCandleResult(
                candle=candle,
                candle_fills=fills,
                on_fill_intents=[],
                tick_intents=[],
                snapshot=None,
                strategy_state=dict(sim.runtime_state.strategy_state),
                same_candle_fill_count=0,
                paired_exit_fills_count=0,
            )

        sim.process_candle = _process_candle  # type: ignore[assignment]
        sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]

        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=len(candles) - 1,
                recovery_bot_config=sim.recovery_bot_config,
            )
        return result, process_calls["count"], sim.recovery_bot_tracker

    def _assert_one_main_phase_per_candle(self, trace: list[dict[str, object]]) -> None:
        phases_by_candle: dict[int, set[str]] = {}
        for entry in trace:
            candle_index = entry.get("candle_index")
            action = str(entry.get("action") or "")
            phase = _main_phase_name(action)
            if candle_index is None or phase is None:
                continue
            phases_by_candle.setdefault(int(candle_index), set()).add(phase)
        for candle_index, phases in phases_by_candle.items():
            self.assertLessEqual(
                len(phases),
                1,
                msg=f"multiple recovery main phases in candle {candle_index}: {sorted(phases)}",
            )

    def test_scenario_a_direct_successful_recovery_close(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=1,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=10.0,
            minimum_pair_qty=10.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=500.0,
            reload_enabled=False,
        )
        sim = self._sim(
            close=100.0,
            long_qty=30.0,
            short_qty=20.0,
            long_avg=100.0,
            short_avg=100.0,
            config=config,
        )
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0},
            {"open": 99.99, "high": 99.99, "low": 99.99, "close": 99.99},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        ]
        result, process_calls, tracker = self._run_with_mocked_sim(
            candles=candles,
            sim=sim,
            trigger_price=100.0,
        )
        self.assertEqual(result.final_status, "closed")
        self.assertEqual(tracker.state, RecoveryState.CLOSED)
        self.assertEqual(tracker.reload_count, 0)
        self.assertEqual(process_calls, 1)
        self.assertFalse(result.final_active_orders)
        self.assertEqual(collect_recovery_invariant_violations(sim, tracker), [])
        self._assert_one_main_phase_per_candle(result.recovery_trace)
        self.assertFalse(any("RELOAD" in str(entry.get("action")) for entry in result.recovery_trace))

    def test_scenario_b_one_reload_then_successful_close(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=1,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=5.0,
            minimum_pair_qty=5.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=98.0,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=1,
            reload_long_notional_usdt=475.0,
            reload_short_notional_usdt=237.5,
        )
        sim = self._sim(
            close=100.0,
            long_qty=5.0,
            short_qty=5.0,
            long_avg=100.0,
            short_avg=90.0,
            config=config,
            fee_rate=0.0,
        )
        tracker = sim.recovery_bot_tracker
        assert tracker is not None
        tracker.state = RecoveryState.WAITING_FOR_RELOAD
        tracker.waiting_for_reload_since_candle_index = 0
        tracker.loss_budget_usdt = 55.0
        tracker.loss_budget_used_usdt = 0.0
        tracker.recovery_start_candle_index = 0
        candles = [
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 94.0, "high": 94.0, "low": 94.0, "close": 94.0},
            {"open": 94.94, "high": 94.94, "low": 94.94, "close": 94.94},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        ]
        result, process_calls, tracker = self._run_with_mocked_sim(
            candles=candles,
            sim=sim,
            trigger_price=100.0,
            emit_trigger=False,
        )
        self.assertEqual(result.final_status, "closed")
        self.assertEqual(tracker.state, RecoveryState.CLOSED)
        self.assertEqual(tracker.reload_count, 1)
        self.assertEqual(process_calls, 0)
        self._assert_one_main_phase_per_candle(result.recovery_trace)
        reload_actions = [entry for entry in result.recovery_trace if entry.get("action") == "RELOAD_FILLED"]
        self.assertEqual(len(reload_actions), 1)
        self.assertEqual(collect_recovery_invariant_violations(sim, tracker), [])

    def test_scenario_c_max_reloads_reached_stops_loop(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=1,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=5.0,
            minimum_pair_qty=5.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=90.0,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=1,
            reload_long_notional_usdt=475.0,
            reload_short_notional_usdt=237.5,
        )
        sim = self._sim(
            close=100.0,
            long_qty=15.0,
            short_qty=10.0,
            long_avg=100.0,
            short_avg=90.0,
            config=config,
            fee_rate=0.0,
        )
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0},
            {"open": 99.99, "high": 99.99, "low": 99.99, "close": 99.99},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 94.0, "high": 94.0, "low": 94.0, "close": 94.0},
            {"open": 94.94, "high": 94.94, "low": 94.94, "close": 94.94},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        ]
        result, process_calls, tracker = self._run_with_mocked_sim(
            candles=candles,
            sim=sim,
            trigger_price=100.0,
        )
        self.assertEqual(process_calls, 1)
        self.assertEqual(tracker.reload_count, 1)
        self.assertEqual(tracker.state, RecoveryState.WAITING_FOR_RELOAD)
        self.assertEqual(tracker.blocked_reason, "max_recovery_reloads_reached")
        self._assert_one_main_phase_per_candle(result.recovery_trace)

    def test_scenario_d_final_exit_partial_fill_leads_failed(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=1,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=10.0,
            minimum_pair_qty=10.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=500.0,
        )
        sim = self._sim(
            close=100.0,
            long_qty=30.0,
            short_qty=20.0,
            long_avg=100.0,
            short_avg=100.0,
            config=config,
        )
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0},
            {"open": 99.99, "high": 99.99, "low": 99.99, "close": 99.99},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        ]
        original_fill = __import__("research.backtests.recovery_bot.engine", fromlist=["fill_order_at_candle_close"]).fill_order_at_candle_close

        def _side_effect(*, book, runtime_state, order_id, candle):
            order = book.get_order(order_id)
            if order is not None and str(order.purpose or "") == RECOVERY_FINAL_EXIT_SHORT_PURPOSE:
                raise RuntimeError("forced short final exit failure")
            return original_fill(book=book, runtime_state=runtime_state, order_id=order_id, candle=candle)

        with mock.patch("research.backtests.recovery_bot.engine.fill_order_at_candle_close", side_effect=_side_effect):
            result, _process_calls, tracker = self._run_with_mocked_sim(
                candles=candles,
                sim=sim,
                trigger_price=100.0,
            )
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "final_exit_atomicity_failed")
        later_actions = [entry for entry in result.recovery_trace if entry.get("candle_index") and int(entry["candle_index"]) > 5]
        self.assertFalse(any(str(entry.get("action")).endswith("_SUBMITTED") for entry in later_actions))

    def test_scenario_e_reload_partial_fill_leads_failed(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=1,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=5.0,
            minimum_pair_qty=5.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=98.0,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=1,
            reload_long_notional_usdt=475.0,
            reload_short_notional_usdt=237.5,
        )
        sim = self._sim(
            close=100.0,
            long_qty=15.0,
            short_qty=10.0,
            long_avg=100.0,
            short_avg=90.0,
            config=config,
        )
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0},
            {"open": 99.99, "high": 99.99, "low": 99.99, "close": 99.99},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
            {"open": 94.0, "high": 94.0, "low": 94.0, "close": 94.0},
            {"open": 94.94, "high": 94.94, "low": 94.94, "close": 94.94},
        ]
        original_fill = __import__("research.backtests.recovery_bot.engine", fromlist=["fill_order_at_price"]).fill_order_at_price

        def _side_effect(*, book, runtime_state, order_id, fill_price, occurred_at=None, touch_metadata=None):
            order = book.get_order(order_id)
            if order is not None and str(order.purpose or "") == RECOVERY_RELOAD_SHORT_PURPOSE:
                raise RuntimeError("forced short reload failure")
            return original_fill(
                book=book,
                runtime_state=runtime_state,
                order_id=order_id,
                fill_price=fill_price,
                occurred_at=occurred_at,
                touch_metadata=touch_metadata,
            )

        with mock.patch("research.backtests.recovery_bot.engine.fill_order_at_price", side_effect=_side_effect):
            result, _process_calls, tracker = self._run_with_mocked_sim(
                candles=candles,
                sim=sim,
                trigger_price=100.0,
            )
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "recovery_reload_atomicity_failed")
        later_actions = [entry for entry in result.recovery_trace if entry.get("candle_index") and int(entry["candle_index"]) > 6]
        self.assertFalse(any(str(entry.get("action")).endswith("_SUBMITTED") for entry in later_actions))

    def test_scenario_f_untradeable_pair_reduction_dust_fails_stably(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_wait_candles=0,
            trigger_price_drop_pct=0.0,
            neutralize_step_price_drop_pct=1.0,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=1,
            pair_reduce_move_pct=1.0,
            pair_reduce_mode="fixed_qty",
            pair_reduce_qty=0.05,
            minimum_pair_qty=5.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=500.0,
        )
        sim = self._sim(
            close=100.0,
            long_qty=11.0,
            short_qty=6.0,
            long_avg=100.0,
            short_avg=100.0,
            config=config,
            qty_step=0.1,
        )
        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.0, "high": 99.0, "low": 99.0, "close": 99.0},
            {"open": 99.99, "high": 99.99, "low": 99.99, "close": 99.99},
            {"open": 99.99, "high": 99.99, "low": 99.99, "close": 99.99},
        ]
        result, _process_calls, tracker = self._run_with_mocked_sim(
            candles=candles,
            sim=sim,
            trigger_price=100.0,
        )
        self.assertEqual(tracker.state, RecoveryState.FAILED)
        self.assertEqual(tracker.blocked_reason, "pair_reduction_untradeable")
        fail_entries = [entry for entry in result.recovery_trace if entry.get("action") == "RECOVERY_FAILED"]
        self.assertEqual(len(fail_entries), 1)

    def test_scenario_g_already_flat_ready_to_close(self) -> None:
        config = RecoveryBotConfig(enabled=True, loss_budget_mode="fixed", fixed_loss_budget_usdt=10.0)
        sim = self._sim(
            close=100.0,
            long_qty=0.0,
            short_qty=0.0,
            long_avg=0.0,
            short_avg=0.0,
            config=config,
        )
        tracker = sim.recovery_bot_tracker
        assert tracker is not None
        tracker.state = RecoveryState.READY_TO_CLOSE
        sim.process_candle = lambda candle, **_kwargs: ProcessCandleResult(  # type: ignore[assignment]
            candle=candle,
            candle_fills=[],
            on_fill_intents=[],
            tick_intents=[],
            snapshot=None,
            strategy_state=dict(sim.runtime_state.strategy_state),
            same_candle_fill_count=0,
            paired_exit_fills_count=0,
        )
        sim.run_entry_smoke = lambda: type("EntryResult", (), {"entry_fills": []})()  # type: ignore[assignment]

        candles = [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
        ]
        with mock.patch("research.backtests.historical_backtest.HedgeBotOriginalSimulator") as mock_sim_cls:
            mock_sim_cls.return_value = sim
            result = run_historical_backtest(
                "BTCUSDT",
                "long",
                candles,
                max_candles=1,
                recovery_bot_config=config,
            )
        self.assertEqual(result.final_status, "closed")
        self.assertEqual(tracker.state, RecoveryState.CLOSED)
        self.assertEqual(tracker.final_exit_reason, "already_flat")
        self.assertFalse([row for row in result.fill_log if str(row.get("purpose") or "").startswith("RECOVERY_FINAL_EXIT_")])


if __name__ == "__main__":
    unittest.main()
