#!/usr/bin/env python3
from __future__ import annotations

import unittest

from fixed_cycle_hedge_bot.hedge_exit_math import calculate_hedge_exit_price


class HedgeExitMathTests(unittest.TestCase):
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
        )
        self.assertGreater(with_pending.exit_price, base.exit_price)
        self.assertAlmostEqual(
            with_pending.required_profit_usdt - base.required_profit_usdt,
            1.13,
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
        )
        self.assertLess(with_profit.required_profit_usdt, neutral.required_profit_usdt)
        self.assertAlmostEqual(
            neutral.required_profit_usdt - with_profit.required_profit_usdt,
            0.75,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
