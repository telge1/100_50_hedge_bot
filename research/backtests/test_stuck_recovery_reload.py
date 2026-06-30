"""Tests for backtest-only stuck recovery reload."""

from __future__ import annotations

import json
import unittest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig
from fixed_cycle_hedge_bot.models import StrategyIntent
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_execution import is_stuck_recovery_reload_market_fill
from research.backtests.simulated_order_book import SyntheticCandle, VirtualOrder
from research.backtests.stuck_recovery_reload import (
    StuckRecoveryReloadConfig,
    config_from_json_string,
    default_stuck_recovery_reload_config,
    extract_cycle_index_from_short_reduce_purpose,
    is_cycle_short_reduce_purpose,
    resolve_reload_notionals,
    should_trigger_stuck_recovery_reload,
)
from research.backtests.stuck_recovery_reload_shim import (
    StuckRecoveryReloadTracker,
    execute_stuck_recovery_reload,
    maybe_execute_stuck_recovery_reload,
)


def _virtual_order(*, order_id: str, purpose: str, side: str = "short", qty: float = 10.0) -> VirtualOrder:
    return VirtualOrder(
        order_id=order_id,
        exchange_order_id=f"ex-{order_id}",
        symbol="APTUSDT",
        side=side,
        qty=qty,
        price=9.0,
        trigger_price=9.0,
        trigger_direction=2,
        order_type="Limit",
        reduce_only=True,
        purpose=purpose,
        status="OPEN",
    )


def _apt_sim(*, close: float = 10.0, reload_config: StuckRecoveryReloadConfig | None = None) -> HedgeBotOriginalSimulator:
    config = FixedCycleHedgeConfig(
        bot_name="long_bot_1",
        strategy_side="long",
        symbol="APTUSDT",
        restart=False,
        base_notional_usdt=100.0,
        hedge_ratio_short=0.5,
        qty_step=0.01,
        min_order_qty=0.01,
        min_notional_usdt=5.0,
        price_tick_size=0.0001,
    )
    return HedgeBotOriginalSimulator(
        signal="long",
        symbol="APTUSDT",
        candle_close=close,
        config=config,
        stuck_recovery_reload_config=reload_config,
    )


class StuckRecoveryReloadHelperTests(unittest.TestCase):
    def test_is_cycle_short_reduce_purpose(self) -> None:
        self.assertTrue(is_cycle_short_reduce_purpose("CYCLE_5_SHORT_REDUCE"))
        self.assertFalse(is_cycle_short_reduce_purpose("LONG_TP_EXIT"))
        self.assertFalse(is_cycle_short_reduce_purpose("CYCLE_5_LONG_ADD"))

    def test_extract_cycle_index(self) -> None:
        self.assertEqual(extract_cycle_index_from_short_reduce_purpose("CYCLE_6_SHORT_REDUCE"), 6)

    def test_config_from_json_string(self) -> None:
        payload = {
            "enabled": True,
            "reload_min_cycle_index": 6,
            "reload_wait_candles_after_last_fill": 300,
            "max_reloads_per_trade": 2,
            "name": "epoch_001",
        }
        config = config_from_json_string(json.dumps(payload))
        self.assertTrue(config.enabled)
        self.assertEqual(config.reload_min_cycle_index, 6)
        self.assertEqual(config.max_reloads_per_trade, 2)
        self.assertEqual(config.name, "epoch_001")

    def test_resolve_reload_notionals_fallback(self) -> None:
        config = StuckRecoveryReloadConfig(enabled=True)
        long_n, short_n = resolve_reload_notionals(config, {}, FixedCycleHedgeConfig())
        self.assertAlmostEqual(long_n, 100.0)
        self.assertAlmostEqual(short_n, 50.0)

    def test_resolve_reload_notionals_from_initial_qty(self) -> None:
        config = StuckRecoveryReloadConfig(enabled=True)
        state = {
            "entry_reference_price": 10.0,
            "initial_long_qty": 12.0,
            "initial_short_qty": 6.0,
        }
        long_n, short_n = resolve_reload_notionals(config, state, FixedCycleHedgeConfig())
        self.assertAlmostEqual(long_n, 120.0)
        self.assertAlmostEqual(short_n, 60.0)


class StuckRecoveryReloadTriggerTests(unittest.TestCase):
    def _sim_with_short_reduce(self, purpose: str = "CYCLE_5_SHORT_REDUCE") -> HedgeBotOriginalSimulator:
        sim = _apt_sim()
        sim.book.long_qty = 100.0
        sim.book.short_qty = 50.0
        sim.book.long_avg = 10.0
        sim.book.short_avg = 10.0
        order = _virtual_order(order_id="cycle-order", purpose=purpose)
        sim.book._orders[order.order_id] = order
        return sim

    def test_trigger_only_for_short_reduce(self) -> None:
        config = default_stuck_recovery_reload_config()
        sim = self._sim_with_short_reduce("CYCLE_5_SHORT_REDUCE")
        should, trigger = should_trigger_stuck_recovery_reload(
            sim,
            config=config,
            cumulative_pnl=-0.5,
            candles_since_last_fill=500,
            reload_count_for_trade=0,
            trade_closed=False,
        )
        self.assertTrue(should)
        assert trigger is not None
        self.assertEqual(trigger.cycle_index, 5)

    def test_no_trigger_for_exit_orders(self) -> None:
        config = default_stuck_recovery_reload_config()
        sim = _apt_sim()
        sim.book._orders["exit-long"] = _virtual_order(
            order_id="exit-long",
            purpose="LONG_TP_EXIT",
            side="long",
        )
        sim.book._orders["exit-short"] = _virtual_order(
            order_id="exit-short",
            purpose="SHORT_SL_EXIT",
            side="short",
        )
        should, _ = should_trigger_stuck_recovery_reload(
            sim,
            config=config,
            cumulative_pnl=-1.0,
            candles_since_last_fill=1000,
            reload_count_for_trade=0,
            trade_closed=False,
        )
        self.assertFalse(should)

    def test_no_trigger_before_min_cycle(self) -> None:
        config = default_stuck_recovery_reload_config()
        sim = self._sim_with_short_reduce("CYCLE_4_SHORT_REDUCE")
        should, _ = should_trigger_stuck_recovery_reload(
            sim,
            config=config,
            cumulative_pnl=-1.0,
            candles_since_last_fill=1000,
            reload_count_for_trade=0,
            trade_closed=False,
        )
        self.assertFalse(should)

    def test_no_trigger_when_pnl_non_negative(self) -> None:
        config = default_stuck_recovery_reload_config()
        sim = self._sim_with_short_reduce()
        should, _ = should_trigger_stuck_recovery_reload(
            sim,
            config=config,
            cumulative_pnl=0.0,
            candles_since_last_fill=1000,
            reload_count_for_trade=0,
            trade_closed=False,
        )
        self.assertFalse(should)

    def test_max_reloads_respected(self) -> None:
        config = default_stuck_recovery_reload_config()
        sim = self._sim_with_short_reduce()
        should, _ = should_trigger_stuck_recovery_reload(
            sim,
            config=config,
            cumulative_pnl=-1.0,
            candles_since_last_fill=1000,
            reload_count_for_trade=1,
            trade_closed=False,
        )
        self.assertFalse(should)


class StuckRecoveryReloadExecutionTests(unittest.TestCase):
    def test_stuck_reload_market_intents_are_immediate(self) -> None:
        intent = StrategyIntent(
            side="long",
            qty=1.0,
            purpose="STUCK_RECOVERY_RELOAD_LONG_ENTRY",
            order_type="Market",
        )
        self.assertTrue(is_stuck_recovery_reload_market_fill(intent))

    def test_execute_reload_cancels_old_orders_and_adds_positions(self) -> None:
        sim = _apt_sim(close=10.0, reload_config=default_stuck_recovery_reload_config())
        state = sim.runtime_state.strategy_state
        state.update(
            {
                "trade_block_id": "tb-stuck",
                "initial_entry_confirmed": True,
                "initial_structure_built": True,
                "initial_long_qty": 100.0,
                "initial_short_qty": 50.0,
                "entry_reference_price": 10.0,
                "active_cycle_index": 5,
            }
        )
        sim.book.long_qty = 80.0
        sim.book.short_qty = 40.0
        sim.book.long_avg = 10.0
        sim.book.short_avg = 10.0
        exit_order = _virtual_order(order_id="exit-long", purpose="LONG_TP_EXIT", side="long", qty=80.0)
        cycle_order = _virtual_order(order_id="cycle-5", purpose="CYCLE_5_SHORT_REDUCE")
        sim.book._orders[exit_order.order_id] = exit_order
        sim.book._orders[cycle_order.order_id] = cycle_order
        sim.book.sync_runtime_state(sim.runtime_state)
        sim._refresh_snapshot_from_book(source="setup", price=10.0)

        from research.backtests.stuck_recovery_reload import StuckRecoveryReloadTrigger

        trigger = StuckRecoveryReloadTrigger(
            cycle_index=5,
            active_purpose="CYCLE_5_SHORT_REDUCE",
            candles_since_last_fill=500,
            realized_pnl_before=-0.5,
        )
        fills, record = execute_stuck_recovery_reload(
            sim,
            config=default_stuck_recovery_reload_config(),
            trigger=trigger,
            reload_count_for_trade=1,
        )
        self.assertEqual(len(fills), 2)
        self.assertTrue(record.stuck_recovery_reload_triggered)
        self.assertNotIn("cycle-5", {order.order_id for order in sim.book.active_orders()})
        self.assertGreater(sim.book.long_qty, 80.0)
        self.assertGreater(record.reload_long_qty or 0.0, 0.0)


class StuckRecoveryReloadBaselineTests(unittest.TestCase):
    def test_disabled_config_does_not_attach_tracker(self) -> None:
        sim = _apt_sim(reload_config=StuckRecoveryReloadConfig(enabled=False))
        self.assertIsNone(sim.stuck_recovery_reload_tracker)
        sim.close()

    def test_baseline_without_flag_is_unchanged_between_runs(self) -> None:
        candles = [
            SyntheticCandle(symbol="APTUSDT", close=price, open=price, high=price, low=price)
            for price in [10.0, 10.1, 9.9, 10.05, 9.95, 10.2, 10.0, 9.8, 10.1, 10.3]
        ]
        first = run_historical_backtest(
            "APTUSDT",
            "long",
            candles,
            max_candles=len(candles) - 1,
            config_source="test",
        )
        second = run_historical_backtest(
            "APTUSDT",
            "long",
            candles,
            max_candles=len(candles) - 1,
            config_source="test",
        )
        self.assertEqual(first.final_status, second.final_status)
        self.assertEqual(first.realized_pnl, second.realized_pnl)
        self.assertEqual(first.fills_count, second.fills_count)

    def test_maybe_execute_returns_empty_when_disabled(self) -> None:
        sim = _apt_sim()
        tracker = StuckRecoveryReloadTracker(config=StuckRecoveryReloadConfig(enabled=False))
        fills = maybe_execute_stuck_recovery_reload(
            sim,
            tracker,
            cumulative_pnl=-1.0,
            candle_index=1000,
            trade_closed=False,
        )
        self.assertEqual(fills, [])
        sim.close()


if __name__ == "__main__":
    unittest.main()
