from __future__ import annotations

import unittest

from research.backtests.recovery_bot.calculations import (
    compute_loss_budget_usdt,
    compute_neutralization_fixed_step_qty,
    compute_neutralization_step_qty,
    compute_net_long_qty,
    compute_pair_reduce_step_qty,
    compute_price_drop_pct,
    compute_signed_price_move_pct,
    is_cycle_short_reduce_purpose,
    is_exactly_neutral,
    matches_configured_trigger,
    would_exceed_loss_budget,
)
from research.backtests.recovery_bot.config import RecoveryBotConfig


class RecoveryBotCalculationsTests(unittest.TestCase):
    def test_is_cycle_short_reduce_purpose_and_extract(self) -> None:
        self.assertTrue(is_cycle_short_reduce_purpose("CYCLE_3_SHORT_REDUCE"))
        self.assertFalse(is_cycle_short_reduce_purpose("CYCLE_3_LONG_REDUCE"))
        self.assertFalse(is_cycle_short_reduce_purpose("FOO"))

    def test_matches_configured_trigger_exact_match_only(self) -> None:
        self.assertTrue(
            matches_configured_trigger(
                fill_purpose="CYCLE_3_SHORT_REDUCE",
                configured_trigger="CYCLE_3_SHORT_REDUCE",
            )
        )
        self.assertFalse(
            matches_configured_trigger(
                fill_purpose="CYCLE_2_SHORT_REDUCE",
                configured_trigger="CYCLE_3_SHORT_REDUCE",
            )
        )

    def test_compute_price_drop_pct(self) -> None:
        self.assertEqual(compute_price_drop_pct(100.0, 100.0), 0.0)
        self.assertAlmostEqual(compute_price_drop_pct(90.0, 100.0), 10.0, places=6)
        self.assertEqual(compute_price_drop_pct(110.0, 100.0), 0.0)

    def test_compute_signed_price_move_pct(self) -> None:
        self.assertAlmostEqual(
            compute_signed_price_move_pct(110.0, 100.0),
            10.0,
            places=6,
        )
        self.assertAlmostEqual(
            compute_signed_price_move_pct(90.0, 100.0),
            -10.0,
            places=6,
        )

    def test_loss_budget_fixed_profit_share_and_hybrid(self) -> None:
        fixed_cfg = RecoveryBotConfig(
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=3.0,
            minimum_loss_budget_usdt=0.0,
            maximum_loss_budget_usdt=None,
        )
        self.assertAlmostEqual(compute_loss_budget_usdt(fixed_cfg), 3.0, places=6)

        profit_cfg = RecoveryBotConfig(
            loss_budget_mode="profit_share",
            available_profit_pool_usdt=10.0,
            loss_budget_profit_share_pct=20.0,
            minimum_loss_budget_usdt=0.0,
            maximum_loss_budget_usdt=None,
        )
        self.assertAlmostEqual(compute_loss_budget_usdt(profit_cfg), 2.0, places=6)

        hybrid_cfg = RecoveryBotConfig(
            loss_budget_mode="hybrid",
            available_profit_pool_usdt=10.0,
            loss_budget_profit_share_pct=20.0,
            minimum_loss_budget_usdt=1.0,
            maximum_loss_budget_usdt=1.5,
        )
        # Profit share would be 2.0, but clamped to [1.0, 1.5].
        self.assertAlmostEqual(compute_loss_budget_usdt(hybrid_cfg), 1.5, places=6)

    def test_neutralization_fixed_step_and_last_step(self) -> None:
        # Long 120 / Short 70 / Net 50 / 5 steps => 10 per step.
        net = compute_net_long_qty(120.0, 70.0)
        self.assertAlmostEqual(net, 50.0, places=6)
        fixed_step = compute_neutralization_fixed_step_qty(net, 5)
        self.assertAlmostEqual(fixed_step, 10.0, places=6)

        long_qty = 120.0
        short_qty = 70.0
        steps = []
        for _ in range(5):
            step_qty = compute_neutralization_step_qty(long_qty, short_qty, fixed_step)
            steps.append(step_qty)
            long_qty -= step_qty
        self.assertAlmostEqual(long_qty, short_qty, places=6)
        # All steps must be non-negative and the last one must not exceed the
        # remaining net-long qty.
        for qty in steps:
            self.assertGreaterEqual(qty, 0.0)

    def test_is_exactly_neutral_with_tolerance(self) -> None:
        self.assertTrue(is_exactly_neutral(10.0, 10.0, tolerance_qty=0.0))
        self.assertFalse(is_exactly_neutral(10.0, 10.001, tolerance_qty=0.0))
        self.assertTrue(is_exactly_neutral(10.0, 10.001, tolerance_qty=0.01))

    def test_pair_reduction_respects_minimum_pair_qty(self) -> None:
        # Long 70 / Short 70 / minimum pair qty 50, fixed step 10.
        step = compute_pair_reduce_step_qty(
            70.0,
            70.0,
            minimum_pair_qty=50.0,
            mode="fixed_qty",
            fixed_qty=10.0,
            pct=None,
        )
        # We can reduce at most 20 (down to 50).
        self.assertAlmostEqual(step, 10.0, places=6)

        # If we are already at minimum, no further reduction.
        step2 = compute_pair_reduce_step_qty(
            50.0,
            50.0,
            minimum_pair_qty=50.0,
            mode="fixed_qty",
            fixed_qty=10.0,
            pct=None,
        )
        self.assertEqual(step2, 0.0)

    def test_pair_reduction_never_undershoots_minimum(self) -> None:
        # Long/Short 55, minimum 50, fixed step 10 -> should clamp to 5.
        step = compute_pair_reduce_step_qty(
            55.0,
            55.0,
            minimum_pair_qty=50.0,
            mode="fixed_qty",
            fixed_qty=10.0,
            pct=None,
        )
        self.assertAlmostEqual(step, 5.0, places=6)

    def test_would_exceed_loss_budget(self) -> None:
        # Budget 2, used 1.5, step 0.6 -> exceeds.
        self.assertTrue(
            would_exceed_loss_budget(
                loss_budget_usdt=2.0,
                loss_budget_used_usdt=1.5,
                projected_additional_loss_usdt=0.6,
            )
        )
        # Same setup, smaller step -> does not exceed.
        self.assertFalse(
            would_exceed_loss_budget(
                loss_budget_usdt=2.0,
                loss_budget_used_usdt=1.5,
                projected_additional_loss_usdt=0.4,
            )
        )


if __name__ == "__main__":
    unittest.main()

