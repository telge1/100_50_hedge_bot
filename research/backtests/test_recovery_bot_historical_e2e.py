from __future__ import annotations

import os
from pathlib import Path
import unittest

from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.recovery_bot.config import RecoveryBotConfig


REPO_ROOT = Path(__file__).resolve().parents[2]

APT_FEATHER_PATH = Path(
    os.environ.get(
        "APT_FEATHER_PATH",
        str(REPO_ROOT / "APT_USDT_USDT-5m-futures.feather"),
    )
)
APT_RECOVERY_START_INDEX = 8000
APT_RECOVERY_WINDOW = 5000


def _phase_set_for_candle(trace: list[dict[str, object]], candle_index: int) -> set[str]:
    phases: set[str] = set()
    for entry in trace:
        if int(entry.get("candle_index") or -1) != candle_index:
            continue
        action = str(entry.get("action") or "")
        if action.startswith("NEUTRALIZATION_"):
            phases.add("NEUTRALIZATION")
        elif action.startswith("PAIR_REDUCTION_"):
            phases.add("PAIR_REDUCTION")
        elif action.startswith("FINAL_EXIT_"):
            phases.add("FINAL_EXIT")
        elif action in {"RELOAD_SUBMITTED", "RELOAD_FILLED"}:
            phases.add("RELOAD")
    return phases


class RecoveryBotHistoricalE2ETests(unittest.TestCase):
    maxDiff = None

    def _load_apt_rows(self) -> list[dict[str, object]]:
        if not APT_FEATHER_PATH.exists():
            self.skipTest(f"APT feather file missing: {APT_FEATHER_PATH}")
        try:
            from research.backtests.candle_loader import load_candles
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"candle loader unavailable: {exc}")
        try:
            return load_candles(APT_FEATHER_PATH)
        except ImportError as exc:
            self.skipTest(str(exc))

    def _window_rows(self, start: int = APT_RECOVERY_START_INDEX, window: int = APT_RECOVERY_WINDOW):
        rows = self._load_apt_rows()
        if start < 0 or start + window > len(rows):
            self.skipTest(f"APT recovery window out of bounds: start={start} window={window} rows={len(rows)}")
        return rows[start : start + window]

    def test_historical_scenario_1_trigger_pair_reduce_and_close(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_5_SHORT_REDUCE",
            trigger_price_drop_pct=0.0,
            trigger_wait_candles=0,
            neutralize_step_price_drop_pct=0.5,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=3,
            pair_reduce_move_pct=0.5,
            pair_reduce_mode="percent",
            pair_reduce_pct=25.0,
            minimum_pair_qty=20.0,
            minimum_pair_notional_usdt=0.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=20.0,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=1,
            reload_long_notional_usdt=100.0,
            reload_short_notional_usdt=50.0,
            reload_max_total_notional_usdt=200.0,
        )
        rows = self._window_rows()
        result = run_historical_backtest(
            "APTUSDT",
            "long",
            rows,
            max_candles=len(rows) - 1,
            config_source="live",
            recovery_bot_config=config,
        )
        self.assertEqual(result.final_status, "closed")
        self.assertTrue(any(entry.get("action") == "RECOVERY_TRIGGERED" for entry in result.recovery_trace))
        self.assertTrue(any(entry.get("action") == "PAIR_REDUCTION_FILLED" for entry in result.recovery_trace))
        self.assertTrue(any(entry.get("action") == "FINAL_EXIT_FILLED" for entry in result.recovery_trace))

    def test_historical_scenario_2_budget_block_then_reload(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_5_SHORT_REDUCE",
            trigger_price_drop_pct=0.0,
            trigger_wait_candles=0,
            neutralize_step_price_drop_pct=0.5,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=3,
            pair_reduce_move_pct=0.5,
            pair_reduce_mode="percent",
            pair_reduce_pct=25.0,
            minimum_pair_qty=13.5,
            minimum_pair_notional_usdt=0.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=1.07,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=1,
            reload_long_notional_usdt=100.0,
            reload_short_notional_usdt=50.0,
            reload_max_total_notional_usdt=200.0,
        )
        rows = self._window_rows()
        result = run_historical_backtest(
            "APTUSDT",
            "long",
            rows,
            max_candles=len(rows) - 1,
            config_source="live",
            recovery_bot_config=config,
        )
        self.assertTrue(any(entry.get("action") == "FINAL_EXIT_BLOCKED" for entry in result.recovery_trace))
        reload_filled = [entry for entry in result.recovery_trace if entry.get("action") == "RELOAD_FILLED"]
        self.assertTrue(any(entry.get("action") == "RELOAD_FILLED" for entry in result.recovery_trace))
        self.assertEqual((result.recovery_summary or {}).get("reload_count"), 1)
        self.assertEqual(len(reload_filled), 1)

    def test_historical_scenario_3_no_duplicate_main_phase_per_candle(self) -> None:
        config = RecoveryBotConfig(
            enabled=True,
            trigger_order="CYCLE_5_SHORT_REDUCE",
            trigger_price_drop_pct=0.0,
            trigger_wait_candles=0,
            neutralize_step_price_drop_pct=0.5,
            neutralize_reduce_mode="fixed_steps",
            neutralize_target_steps=3,
            pair_reduce_move_pct=0.5,
            pair_reduce_mode="percent",
            pair_reduce_pct=25.0,
            minimum_pair_qty=13.5,
            minimum_pair_notional_usdt=0.0,
            loss_budget_mode="fixed",
            fixed_loss_budget_usdt=1.08,
            reload_enabled=True,
            max_reloads_per_trade=1,
            reload_wait_candles=1,
            reload_long_notional_usdt=100.0,
            reload_short_notional_usdt=50.0,
            reload_max_total_notional_usdt=200.0,
        )
        rows = self._window_rows()
        result = run_historical_backtest(
            "APTUSDT",
            "long",
            rows,
            max_candles=len(rows) - 1,
            config_source="live",
            recovery_bot_config=config,
        )
        trace = result.recovery_trace
        self.assertGreaterEqual(len(trace), 5)
        candle_indexes = sorted({int(entry.get("candle_index")) for entry in trace if entry.get("candle_index") is not None})
        for candle_index in candle_indexes:
            self.assertLessEqual(len(_phase_set_for_candle(trace, candle_index)), 1)


if __name__ == "__main__":
    unittest.main()
