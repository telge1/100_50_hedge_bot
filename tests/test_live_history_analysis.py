import unittest
from typing import Any
from unittest.mock import patch

import pymysql

from strategy.market_regime import live_history_analysis
from strategy.market_regime.cli import (
    _run_compare_live_history_command,
    _run_horizon_distribution_command,
)


def _make_row(
    *,
    fast_state: str,
    mid_state: str,
    slow_state: str,
    routed_state: str,
    confidence: float | None,
    decision: str | None,
    entry_allowed: bool,
) -> dict[str, object]:
    return {
        "fast_state": fast_state,
        "mid_state": mid_state,
        "slow_state": slow_state,
        "routed_state": routed_state,
        "confidence": confidence,
        "decision": decision,
        "entry_allowed": entry_allowed,
    }


class LiveHistoryAnalysisTests(unittest.TestCase):
    def test_aggregate_horizon_distribution_counts_states(self) -> None:
        rows = [
            _make_row(
                fast_state="fast_up",
                mid_state="mid_exhaustion_long",
                slow_state="slow_trend_long",
                routed_state="mid_exhaustion_long",
                confidence=0.85,
                decision="ALLOW",
                entry_allowed=True,
            ),
            _make_row(
                fast_state="fast_down",
                mid_state="mid_reversal_setup_short",
                slow_state="slow_range_neutral",
                routed_state="mid_reversal_setup_short",
                confidence=0.62,
                decision="WATCHLIST",
                entry_allowed=False,
            ),
            _make_row(
                fast_state="fast_down",
                mid_state="mid_reversal_setup_short",
                slow_state="slow_range_neutral",
                routed_state="range_unclear",
                confidence=None,
                decision=None,
                entry_allowed=False,
            ),
        ]

        stats = live_history_analysis.aggregate_horizon_distribution(rows)
        self.assertEqual(stats["fast"]["sample_count"], 3)
        self.assertEqual(stats["fast"]["state_distribution"][0]["state"], "fast_down")
        self.assertGreater(stats["fast"]["state_distribution"][0]["percent"], 30)
        self.assertEqual(stats["mid"]["range_unclear_pct"], 33.33)
        self.assertEqual(stats["slow"]["entry_allowed_pct"], 33.33)
        self.assertEqual(stats["mid"]["confidence"]["sample_count"], 2)
        self.assertIn("ALLOW", stats["mid"]["decision_distribution"])

    def test_compare_live_history_returns_deltas(self) -> None:
        live_rows = [
            _make_row(
                fast_state="fast_up",
                mid_state="mid_exhaustion_long",
                slow_state="slow_trend_long",
                routed_state="mid_exhaustion_long",
                confidence=0.85,
                decision="ALLOW",
                entry_allowed=True,
            )
            for _ in range(3)
        ]
        history_rows = [
            _make_row(
                fast_state="fast_down",
                mid_state="mid_exhaustion_long",
                slow_state="slow_trend_long",
                routed_state="mid_exhaustion_long",
                confidence=0.45,
                decision="WATCHLIST",
                entry_allowed=False,
            )
            for _ in range(2)
        ]

        live_stats = live_history_analysis.aggregate_horizon_distribution(live_rows)
        history_stats = live_history_analysis.aggregate_horizon_distribution(history_rows)
        comparison = live_history_analysis.compare_live_history_distributions(live_stats, history_stats)
        fast_delta = comparison["fast"]["deltas"]
        self.assertIsNotNone(fast_delta)
        self.assertEqual(fast_delta["sample_count"], 1)
        self.assertTrue(fast_delta["confidence_delta"]["average"] > 0)
        self.assertTrue(any(item["delta_pct"] != 0 for item in fast_delta["state_delta"]))

    def test_load_history_rows_handles_missing_table(self) -> None:
        fake_store = live_history_analysis.MarketRegimeStore(
            config=live_history_analysis.MarketRegimeDBConfig()
        )
        with patch(
            "strategy.market_regime.live_history_analysis.MarketRegimeStore.load_market_state_live_telemetry_rows",
            side_effect=pymysql.err.ProgrammingError("Table not found"),
        ):
            rows, error = live_history_analysis.load_history_rows(fake_store, "missing_table")
        self.assertEqual(rows, [])
        self.assertIsNotNone(error)


class HorizonCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            _make_row(
                fast_state="fast_up",
                mid_state="mid_exhaustion_long",
                slow_state="slow_trend_long",
                routed_state="mid_exhaustion_long",
                confidence=0.75,
                decision="ALLOW",
                entry_allowed=True,
            ),
            _make_row(
                fast_state="fast_down",
                mid_state="mid_reversal_setup_short",
                slow_state="slow_range_neutral",
                routed_state="mid_reversal_setup_short",
                confidence=0.60,
                decision="WATCHLIST",
                entry_allowed=False,
            ),
        ]

    class FakeStore:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self._rows = rows

        def load_market_state_live_telemetry_rows(self, *, symbols=None, limit=None):
            return self._rows

    def test_horizon_distribution_command(self) -> None:
        fake_store = self.FakeStore(self.rows)
        payload = _run_horizon_distribution_command(fake_store, symbols=["BTCUSDT"], limit=10)
        self.assertTrue(payload["ok"])
        self.assertIn("fast", payload["horizons"])
        self.assertEqual(payload["total_rows"], 2)

    def test_compare_live_history_command_missing_history(self) -> None:
        fake_store = self.FakeStore(self.rows)
        with patch(
            "strategy.market_regime.cli.load_history_rows",
            return_value=([], "table-missing"),
        ):
            payload = _run_compare_live_history_command(
                fake_store,
                symbols=None,
                limit=None,
                history_table="missing_table",
            )
        self.assertFalse(payload["history_available"])
        self.assertEqual(payload["history_error"], "table-missing")
