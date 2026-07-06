from __future__ import annotations

import json
import unittest

from research.backtests.recovery_bot.config import (
    RecoveryBotConfig,
    config_from_dict,
    config_from_json_string,
    config_to_dict,
    validate_config,
)
from research.backtests.recovery_bot.calculations import compute_loss_budget_usdt


class RecoveryBotConfigTests(unittest.TestCase):
    def test_default_config_constructible_and_disabled(self) -> None:
        # Default config must be constructible without raising and disabled.
        cfg = RecoveryBotConfig()
        self.assertFalse(cfg.enabled)

    def test_default_pair_reduce_mode_is_valid(self) -> None:
        # The default pair_reduce_* combination must be self-consistent.
        cfg = RecoveryBotConfig()
        # validate_config on the dataclass contents must not raise.
        validate_config(cfg.__dict__)
        self.assertEqual(cfg.pair_reduce_mode, "percent")
        self.assertIsNone(cfg.pair_reduce_qty)
        self.assertEqual(cfg.pair_reduce_pct, 10.0)

    def test_valid_trigger_orders(self) -> None:
        payload = {"trigger_order": "CYCLE_2_SHORT_REDUCE"}
        validate_config({**RecoveryBotConfig().__dict__, **payload})

        payload = {"trigger_order": "CYCLE_3_SHORT_REDUCE"}
        validate_config({**RecoveryBotConfig().__dict__, **payload})

    def test_invalid_trigger_order_rejected(self) -> None:
        base = RecoveryBotConfig().__dict__
        with self.assertRaises(ValueError):
            validate_config({**base, "trigger_order": "CYCLE_X_SHORT_REDUCE"})
        with self.assertRaises(ValueError):
            validate_config({**base, "trigger_order": "CYCLE_0_SHORT_REDUCE"})
        with self.assertRaises(ValueError):
            validate_config({**base, "trigger_order": "CYCLE_3_LONG_REDUCE"})

    def test_trigger_matching_only_on_exact_fill_purpose(self) -> None:
        payload = {"trigger_order": "CYCLE_3_SHORT_REDUCE"}
        cfg = config_from_dict({**RecoveryBotConfig().__dict__, **payload})
        self.assertEqual(cfg.trigger_order, "CYCLE_3_SHORT_REDUCE")

    def test_profit_share_budget_from_available_pool(self) -> None:
        cfg = config_from_dict(
            {
                **RecoveryBotConfig().__dict__,
                "loss_budget_mode": "profit_share",
                "available_profit_pool_usdt": 10.0,
                "loss_budget_profit_share_pct": 20.0,
            }
        )
        budget = compute_loss_budget_usdt(cfg)
        self.assertAlmostEqual(budget, 2.0, places=6)

    def test_negative_values_rejected(self) -> None:
        base = RecoveryBotConfig().__dict__
        with self.assertRaises(ValueError):
            validate_config({**base, "trigger_price_drop_pct": -0.1})
        with self.assertRaises(ValueError):
            validate_config({**base, "pair_reduce_move_pct": -0.1})
        with self.assertRaises(ValueError):
            validate_config({**base, "minimum_pair_qty": -1.0})

    def test_neutralize_target_steps_must_be_at_least_one(self) -> None:
        base = RecoveryBotConfig().__dict__
        with self.assertRaises(ValueError):
            validate_config({**base, "neutralize_target_steps": 0})

    def test_pair_reduce_mode_requires_parameters(self) -> None:
        base = RecoveryBotConfig().__dict__
        # fixed_qty without qty
        with self.assertRaises(ValueError):
            validate_config({**base, "pair_reduce_mode": "fixed_qty", "pair_reduce_qty": None})
        # percent without pct
        with self.assertRaises(ValueError):
            validate_config({**base, "pair_reduce_mode": "percent", "pair_reduce_pct": None})
        # percent > 100
        with self.assertRaises(ValueError):
            validate_config({**base, "pair_reduce_mode": "percent", "pair_reduce_pct": 150.0})

    def test_pair_reduce_percent_with_default_pct_is_valid(self) -> None:
        base = RecoveryBotConfig().__dict__
        # Explicitly set mode to percent and provide a sane default pct.
        validate_config({**base, "pair_reduce_mode": "percent", "pair_reduce_pct": 10.0})

    def test_budget_minimum_must_not_exceed_maximum(self) -> None:
        base = RecoveryBotConfig().__dict__
        with self.assertRaises(ValueError):
            validate_config(
                {
                    **base,
                    "minimum_loss_budget_usdt": 5.0,
                    "maximum_loss_budget_usdt": 1.0,
                }
            )

    def test_config_json_roundtrip(self) -> None:
        cfg = RecoveryBotConfig(enabled=True, trigger_order="CYCLE_4_SHORT_REDUCE")
        raw = json.dumps(config_to_dict(cfg))
        cfg2 = config_from_json_string(raw)
        self.assertEqual(cfg2.enabled, cfg.enabled)
        self.assertEqual(cfg2.trigger_order, cfg.trigger_order)

    def test_default_loss_budget_is_zero_without_profit_pool(self) -> None:
        cfg = RecoveryBotConfig()
        budget = compute_loss_budget_usdt(cfg)
        self.assertAlmostEqual(budget, 0.0, places=6)

    def test_reload_enabled_requires_explicit_limits_and_notionals(self) -> None:
        base = RecoveryBotConfig().__dict__
        with self.assertRaises(ValueError):
            validate_config({**base, "reload_enabled": True})
        with self.assertRaises(ValueError):
            validate_config(
                {
                    **base,
                    "reload_enabled": True,
                    "max_reloads_per_trade": 1,
                    "reload_long_notional_usdt": 100.0,
                }
            )

        validate_config(
            {
                **base,
                "reload_enabled": True,
                "max_reloads_per_trade": 1,
                "reload_wait_candles": 2,
                "reload_long_notional_usdt": 100.0,
                "reload_short_notional_usdt": 50.0,
                "reload_slippage_pct": 0.2,
            }
        )

    def test_reload_max_total_notional_must_be_positive_when_set(self) -> None:
        base = RecoveryBotConfig().__dict__
        with self.assertRaises(ValueError):
            validate_config({**base, "reload_max_total_notional_usdt": 0.0})



if __name__ == "__main__":
    unittest.main()

