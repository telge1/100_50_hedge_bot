#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

DASHBOARD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(DASHBOARD_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from app import (  # noqa: E402
    _apply_confirmed_end_profit_to_record,
    _build_confirmed_pnl_index,
    _build_profit_trade_filtered_rows,
    _dedupe_profit_trade_rows_by_trade_block_id,
    _enrich_profit_trade_row_from_confirmed_index,
    _get_confirmed_pnl_rows_for_trade_block,
    _profit_trade_default_sort_key,
    _sort_profit_trade_rows_default,
)

TBID_A = "2b3e3f30-3224-4b6b-8be1-4118197d1d55"
TBID_B = "11111111-1111-1111-1111-111111111111"


class BuildConfirmedPnlIndexTests(unittest.TestCase):
    def test_groups_rows_by_trade_block_id(self) -> None:
        rows = [
            {"trade_block_id": TBID_A, "timestamp": "2026-01-01T10:00:00+00:00", "closed_pnl": 1.0},
            {"trade_block_id": TBID_A, "timestamp": "2026-01-02T10:00:00+00:00", "closed_pnl": 2.0},
            {"trade_block_id": TBID_B, "timestamp": "2026-01-01T11:00:00+00:00", "closed_pnl": 3.0},
        ]
        with mock.patch(
            "app._collect_confirmed_order_pnl_rows_from_paths",
            return_value=rows,
        ) as collect_mock:
            index = _build_confirmed_pnl_index("bot_1", "long")
        collect_mock.assert_called_once()
        self.assertEqual(len(index["by_trade_block_id"][TBID_A]), 2)
        self.assertEqual(len(index["by_trade_block_id"][TBID_B]), 1)
        self.assertEqual(
            index["start_times"][TBID_A],
            datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        )


class ConfirmedEndProfitTests(unittest.TestCase):
    def test_endprofit_is_sum_of_confirmed_rows(self) -> None:
        confirmed_rows = [
            {"trade_block_id": TBID_A, "closed_pnl": 0.5, "purpose": "CYCLE_1_LONG_REDUCE"},
            {"trade_block_id": TBID_A, "closed_pnl": -0.1, "purpose": "CYCLE_1_SHORT_REDUCE"},
            {"trade_block_id": TBID_A, "closed_pnl": 0.2, "purpose": "LONG_TP_EXIT"},
        ]
        record: dict = {"trade_block_id": TBID_A, "status": "closed"}
        end_profit, _pnl_count, _purposes = _apply_confirmed_end_profit_to_record(record, confirmed_rows)
        self.assertAlmostEqual(end_profit, 0.6)
        self.assertEqual(record["confirmed_pnl_row_count"], 3)
        self.assertAlmostEqual(record["profit_usdt"], 0.6)

    def test_enrich_uses_index_without_reloading(self) -> None:
        index = {
            "by_trade_block_id": {
                TBID_A: [
                    {"trade_block_id": TBID_A, "closed_pnl": 1.25, "purpose": "CYCLE_1_LONG_REDUCE"},
                ]
            },
            "start_times": {},
        }
        row = {"trade_block_id": TBID_A, "status": "closed"}
        with mock.patch("app._collect_confirmed_order_pnl_rows_from_paths") as collect_mock:
            self.assertTrue(_enrich_profit_trade_row_from_confirmed_index(row, index))
            collect_mock.assert_not_called()
        self.assertAlmostEqual(row["profit_usdt"], 1.25)
        self.assertEqual(row["confirmed_pnl_row_count"], 1)


class ProfitTradeDefaultSortTests(unittest.TestCase):
    def test_active_rows_sort_before_closed_rows(self) -> None:
        rows = [
            {"trade_block_id": TBID_B, "status": "closed", "end_time": "2026-06-01T12:00:00+00:00"},
            {"trade_block_id": TBID_A, "status": "in_progress", "is_process": True, "start_time": "2026-06-02T08:00:00+00:00"},
        ]
        _sort_profit_trade_rows_default(rows)
        self.assertEqual(rows[0]["trade_block_id"], TBID_A)
        self.assertEqual(rows[1]["trade_block_id"], TBID_B)

    def test_closed_rows_sort_by_start_time_desc(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "status": "closed",
                "start_time": "2026-06-01T12:00:00+00:00",
                "end_time": "2026-06-03T12:00:00+00:00",
            },
            {
                "trade_block_id": TBID_B,
                "status": "closed",
                "start_time": "2026-06-03T12:00:00+00:00",
                "end_time": "2026-06-01T12:00:00+00:00",
            },
        ]
        _sort_profit_trade_rows_default(rows)
        self.assertEqual(rows[0]["trade_block_id"], TBID_B)
        self.assertEqual(rows[1]["trade_block_id"], TBID_A)

    def test_closed_rows_use_end_time_only_as_tiebreaker(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "status": "closed",
                "start_time": "2026-06-03T12:00:00+00:00",
                "end_time": "2026-06-03T14:00:00+00:00",
            },
            {
                "trade_block_id": TBID_B,
                "status": "closed",
                "start_time": "2026-06-03T12:00:00+00:00",
                "end_time": "2026-06-03T16:00:00+00:00",
            },
        ]
        _sort_profit_trade_rows_default(rows)
        self.assertEqual(rows[0]["trade_block_id"], TBID_B)
        self.assertEqual(rows[1]["trade_block_id"], TBID_A)

    def test_closed_rows_without_end_time_fallback_to_start_time(self) -> None:
        older = {
            "trade_block_id": TBID_A,
            "status": "closed",
            "start_time": "2026-06-01T12:00:00+00:00",
        }
        newer = {
            "trade_block_id": TBID_B,
            "status": "closed",
            "timestamp": "2026-06-03T12:00:00+00:00",
        }
        self.assertLess(
            _profit_trade_default_sort_key(newer),
            _profit_trade_default_sort_key(older),
        )


class ProfitTradeFilteredRowsLoadTests(unittest.TestCase):
    def test_confirmed_history_loaded_once_per_page_build(self) -> None:
        sample_rows = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "closed_pnl": 0.4,
                "purpose": "CYCLE_1_LONG_REDUCE",
                "timestamp": "2026-06-01T12:00:00+00:00",
            }
        ]
        base_trades = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "symbol": "NEARUSDT",
                "status": "closed",
                "end_time": "2026-06-01T13:00:00+00:00",
            }
        ]
        with mock.patch(
            "app._collect_confirmed_order_pnl_rows_from_paths",
            return_value=sample_rows,
        ) as collect_mock:
            with mock.patch(
                "app._load_trade_blocks_for_profile",
                return_value=(base_trades, [], {}),
            ):
                with mock.patch(
                    "app._collect_active_bot_process_rows",
                    return_value=([], []),
                ):
                    rows, _warnings, _index = _build_profit_trade_filtered_rows(
                        "bot_1",
                        limit=50,
                        bot_side="long",
                    )
        collect_mock.assert_called_once()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["profit_usdt"], 0.4)
        self.assertEqual(rows[0]["confirmed_pnl_row_count"], 1)


class ProfitTradeDedupeStillWorksTests(unittest.TestCase):
    def test_dedupe_after_enrichment_keeps_one_row_per_trade_block_id(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "status": "closed",
                "profit_usdt": 0.4,
                "confirmed_pnl_row_count": 2,
            },
            {
                "trade_block_id": TBID_A,
                "status": "in_progress",
                "is_process": True,
                "confirmed_pnl_row_count": 2,
            },
        ]
        deduped = _dedupe_profit_trade_rows_by_trade_block_id(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["confirmed_pnl_row_count"], 2)


class ConfirmedIndexLookupTests(unittest.TestCase):
    def test_get_rows_for_trade_block_uses_preloaded_index(self) -> None:
        index = {
            "by_trade_block_id": {
                TBID_A: [{"trade_block_id": TBID_A, "closed_pnl": 0.7, "purpose": "REFILL_LONG"}]
            }
        }
        with mock.patch("app._collect_confirmed_order_pnl_rows_from_paths") as collect_mock:
            rows = _get_confirmed_pnl_rows_for_trade_block(
                "bot_1",
                TBID_A,
                confirmed_index=index,
            )
            collect_mock.assert_not_called()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(float(rows[0]["closed_pnl"]), 0.7)


if __name__ == "__main__":
    unittest.main()
