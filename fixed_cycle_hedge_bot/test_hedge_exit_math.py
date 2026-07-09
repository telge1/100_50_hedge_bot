#!/usr/bin/env python3
from __future__ import annotations

import unittest

from fixed_cycle_hedge_bot.hedge_exit_math import calculate_hedge_exit_price


class HedgeExitMathTests(unittest.TestCase):
    def test_long_primary_profit_basis_and_target(self) -> None:
        components = calculate_hedge_exit_price(
            long_avg=2.0,
            long_qty=50.0,
            short_avg=2.0,
            short_qty=25.0,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.0002,
            realized_cycle_net=0.0,
            primary_side="long",
        )
        self.assertAlmostEqual(components.profit_basis_usdt, 100.0)
        self.assertAlmostEqual(components.target_profit_usdt, 0.25)
        self.assertAlmostEqual(components.buffer_usdt, 100.0 * 0.0002 / 100.0)

    def test_short_primary_profit_basis_and_target(self) -> None:
        components = calculate_hedge_exit_price(
            long_avg=2.0,
            long_qty=25.0,
            short_avg=2.0,
            short_qty=50.0,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.0002,
            realized_cycle_net=0.0,
            primary_side="short",
        )
        self.assertAlmostEqual(components.profit_basis_usdt, 100.0)
        self.assertAlmostEqual(components.target_profit_usdt, 0.25)

    def test_symmetry_for_mirrored_positions(self) -> None:
        long_components = calculate_hedge_exit_price(
            long_avg=2.0,
            long_qty=50.0,
            short_avg=2.0,
            short_qty=25.0,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.0002,
            realized_cycle_net=0.0,
            primary_side="long",
        )
        short_components = calculate_hedge_exit_price(
            long_avg=2.0,
            long_qty=25.0,
            short_avg=2.0,
            short_qty=50.0,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.0002,
            realized_cycle_net=0.0,
            primary_side="short",
        )
        self.assertAlmostEqual(
            long_components.target_profit_usdt,
            short_components.target_profit_usdt,
        )
        self.assertAlmostEqual(long_components.buffer_usdt, short_components.buffer_usdt)

    def test_invalid_primary_side_raises(self) -> None:
        with self.assertRaises(ValueError):
            calculate_hedge_exit_price(
                long_avg=1.0,
                long_qty=100.0,
                short_avg=1.0,
                short_qty=50.0,
                tp_profit_target_pct=0.25,
                tp_buffer_pct=0.0,
                realized_cycle_net=0.0,
                primary_side="both",  # type: ignore[arg-type]
            )

    def test_pending_loss_increases_required_exit_price(self) -> None:
        base = calculate_hedge_exit_price(
            long_avg=1.0,
            long_qty=100.0,
            short_avg=0.95,
            short_qty=80.0,
            tp_profit_target_pct=1.0,
            tp_buffer_pct=0.5,
            realized_cycle_net=0.0,
            pending_cycle_loss_usdt=0.0,
            primary_side="long",
        )
        with_pending = calculate_hedge_exit_price(
            long_avg=1.0,
            long_qty=100.0,
            short_avg=0.95,
            short_qty=80.0,
            tp_profit_target_pct=1.0,
            tp_buffer_pct=0.5,
            realized_cycle_net=-1.13,
            pending_cycle_loss_usdt=1.13,
            primary_side="long",
        )
        self.assertGreater(with_pending.exit_price, base.exit_price)
        self.assertAlmostEqual(
            with_pending.required_profit_usdt - base.required_profit_usdt,
            1.13,
            places=6,
        )

    def test_realized_loss_without_pending_increases_required_exit_price(self) -> None:
        neutral = calculate_hedge_exit_price(
            long_avg=1.0,
            long_qty=100.0,
            short_avg=0.95,
            short_qty=80.0,
            tp_profit_target_pct=1.0,
            tp_buffer_pct=0.5,
            realized_cycle_net=0.0,
            pending_cycle_loss_usdt=0.0,
            primary_side="long",
        )
        with_realized_loss = calculate_hedge_exit_price(
            long_avg=1.0,
            long_qty=100.0,
            short_avg=0.95,
            short_qty=80.0,
            tp_profit_target_pct=1.0,
            tp_buffer_pct=0.5,
            realized_cycle_net=-2.0,
            pending_cycle_loss_usdt=0.0,
            primary_side="long",
        )
        self.assertGreater(with_realized_loss.exit_price, neutral.exit_price)
        self.assertAlmostEqual(
            with_realized_loss.required_profit_usdt - neutral.required_profit_usdt,
            2.0,
            places=6,
        )

    def test_realized_profit_reduces_required_exit_price(self) -> None:
        neutral = calculate_hedge_exit_price(
            long_avg=1.0,
            long_qty=100.0,
            short_avg=0.95,
            short_qty=80.0,
            tp_profit_target_pct=1.0,
            tp_buffer_pct=0.5,
            realized_cycle_net=0.0,
            pending_cycle_loss_usdt=0.0,
            primary_side="long",
        )
        with_profit = calculate_hedge_exit_price(
            long_avg=1.0,
            long_qty=100.0,
            short_avg=0.95,
            short_qty=80.0,
            tp_profit_target_pct=1.0,
            tp_buffer_pct=0.5,
            realized_cycle_net=0.75,
            pending_cycle_loss_usdt=0.0,
            primary_side="long",
        )
        self.assertLess(with_profit.required_profit_usdt, neutral.required_profit_usdt)
        self.assertAlmostEqual(
            neutral.required_profit_usdt - with_profit.required_profit_usdt,
            0.75,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
