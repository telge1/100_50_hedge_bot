from __future__ import annotations

import copy
import unittest

from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig


class RecoveryBotBaselineTests(unittest.TestCase):
    def _build_minimal_candles(self) -> list[dict[str, float]]:
        return [
            {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0},
            {"open": 99.5, "high": 101.0, "low": 99.0, "close": 100.5},
            {"open": 100.5, "high": 102.0, "low": 100.0, "close": 101.0},
        ]

    def test_baseline_identity_with_disabled_config(self) -> None:
        candles = self._build_minimal_candles()

        # Baseline without any recovery config.
        result_none = run_historical_backtest(
            "BTCUSDT",
            "long",
            copy.deepcopy(candles),
            max_candles=2,
        )

        # Explicit disabled config must not change anything.
        disabled_cfg = RecoveryBotConfig(enabled=False)
        result_disabled = run_historical_backtest(
            "BTCUSDT",
            "long",
            copy.deepcopy(candles),
            max_candles=2,
            recovery_bot_config=disabled_cfg,
        )

        # High-level equality on PnL and final status/positions.
        self.assertEqual(result_none.realized_pnl, result_disabled.realized_pnl)
        self.assertEqual(result_none.realized_pnl_pct, result_disabled.realized_pnl_pct)
        self.assertEqual(result_none.final_status, result_disabled.final_status)
        self.assertEqual(result_none.exit_reason, result_disabled.exit_reason)
        self.assertEqual(result_none.final_long_qty, result_disabled.final_long_qty)
        self.assertEqual(result_none.final_short_qty, result_disabled.final_short_qty)

        # Fill-log identity: same number of entries and same key fields.
        self.assertEqual(len(result_none.fill_log), len(result_disabled.fill_log))
        for left, right in zip(result_none.fill_log, result_disabled.fill_log):
            for key in (
                "purpose",
                "side",
                "qty",
                "fill_price",
                "closed_pnl",
                "long_qty_after",
                "short_qty_after",
            ):
                self.assertEqual(
                    left.get(key),
                    right.get(key),
                    msg=f"mismatch in fill_log field {key!r}",
                )

        # Order-log identity: same number of entries and same key fields.
        self.assertEqual(len(result_none.order_log), len(result_disabled.order_log))
        for left, right in zip(result_none.order_log, result_disabled.order_log):
            for key in (
                "event_type",
                "purpose",
                "side",
                "qty",
                "price",
                "trigger_price",
                "reduce_only",
            ):
                self.assertEqual(
                    left.get(key),
                    right.get(key),
                    msg=f"mismatch in order_log field {key!r}",
                )


if __name__ == "__main__":
    unittest.main()

