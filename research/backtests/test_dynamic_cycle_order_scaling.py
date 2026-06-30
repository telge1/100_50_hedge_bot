"""Tests for backtest-only dynamic cycle order scaling."""

from __future__ import annotations

import json
import unittest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig
from research.backtests.dynamic_cycle_order_scaling import (
    config_from_json_string,
    default_dynamic_cycle_order_scaling_config,
    get_cycle_scaling_params,
    scale_cycle_qty,
    scaling_applies,
)
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_order_book import SyntheticCandle


class DynamicCycleOrderScalingConfigTests(unittest.TestCase):
    def test_default_bands_for_selected_cycles(self) -> None:
        config = default_dynamic_cycle_order_scaling_config()
        expectations = {
            1: (0.25, 1.00, 0.50),
            2: (0.25, 1.00, 0.50),
            3: (0.20, 0.85, 0.45),
            4: (0.15, 0.70, 0.40),
            5: (0.10, 0.60, 0.35),
            6: (0.05, 0.50, 0.30),
            10: (0.05, 0.50, 0.30),
        }
        for cycle_index, expected in expectations.items():
            params = get_cycle_scaling_params(config, cycle_index)
            self.assertIsNotNone(params, msg=f"cycle {cycle_index}")
            assert params is not None
            self.assertAlmostEqual(params.target_profit_pct, expected[0], places=6)
            self.assertAlmostEqual(params.cycle_qty_factor, expected[1], places=6)
            self.assertAlmostEqual(params.long_add_distance_pct, expected[2], places=6)

    def test_scaling_applies_from_start_cycle_index(self) -> None:
        config = default_dynamic_cycle_order_scaling_config()
        self.assertFalse(scaling_applies(config, 2))
        self.assertTrue(scaling_applies(config, 3))

    def test_config_from_json_string_overrides_defaults(self) -> None:
        payload = {
            "name": "epoch_001",
            "enabled": True,
            "start_cycle_index": 4,
            "bands": [
                {
                    "min_cycle_index": 4,
                    "max_cycle_index": 4,
                    "target_profit_pct": 0.12,
                    "cycle_qty_factor": 0.55,
                    "long_add_distance_pct": 0.33,
                }
            ],
        }
        config = config_from_json_string(json.dumps(payload))
        self.assertEqual(config.name, "epoch_001")
        self.assertEqual(config.start_cycle_index, 4)
        params = get_cycle_scaling_params(config, 4)
        self.assertIsNotNone(params)
        assert params is not None
        self.assertAlmostEqual(params.target_profit_pct, 0.12)
        self.assertAlmostEqual(params.cycle_qty_factor, 0.55)
        self.assertAlmostEqual(params.long_add_distance_pct, 0.33)

    def test_scale_cycle_qty_respects_min_factor(self) -> None:
        config = default_dynamic_cycle_order_scaling_config()
        config.bands[-1] = config.bands[-1].__class__(
            min_cycle_index=6,
            max_cycle_index=None,
            target_profit_pct=0.05,
            cycle_qty_factor=0.10,
            long_add_distance_pct=0.30,
        )
        scaled = scale_cycle_qty(10.0, config, 6, symbol_rules={"qty_step": 0.01, "min_order_qty": 0.01})
        self.assertAlmostEqual(scaled, 2.5)


class DynamicCycleOrderScalingBaselineTests(unittest.TestCase):
    def test_disabled_shim_does_not_patch_strategy(self) -> None:
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=10.0,
            config=FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                base_notional_usdt=100.0,
                hedge_ratio_short=0.5,
            ),
            dynamic_cycle_scaling_config=None,
        )
        self.assertFalse(
            getattr(sim.strategy, "_backtest_dynamic_cycle_order_scaling_installed", False)
        )

    def test_enabled_shim_patches_builders_only(self) -> None:
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=10.0,
            config=FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
            ),
            dynamic_cycle_scaling_config=default_dynamic_cycle_order_scaling_config(),
        )
        self.assertTrue(
            getattr(sim.strategy, "_backtest_dynamic_cycle_order_scaling_installed", False)
        )
        self.assertTrue(
            getattr(sim.strategy, "_backtest_dcos_stale_split_completion_shim_installed", False)
        )
        sim.close()

    def test_install_with_disabled_config_is_noop(self) -> None:
        config = default_dynamic_cycle_order_scaling_config()
        config.enabled = False
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=10.0,
            dynamic_cycle_scaling_config=config,
        )
        self.assertFalse(
            getattr(sim.strategy, "_backtest_dynamic_cycle_order_scaling_installed", False)
        )
        sim.close()


class DynamicCycleOrderScalingBacktestIdentityTests(unittest.TestCase):
    def _sample_candles(self) -> list[SyntheticCandle]:
        prices = [10.0, 10.1, 9.9, 10.05, 9.95, 10.2, 10.0, 9.8, 10.1, 10.3]
        return [
            SyntheticCandle(symbol="APTUSDT", close=price, open=price, high=price, low=price)
            for price in prices
        ]

    def test_baseline_without_flag_is_unchanged_between_runs(self) -> None:
        candles = self._sample_candles()
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
        self.assertEqual(first.orders_submitted, second.orders_submitted)


if __name__ == "__main__":
    unittest.main()
