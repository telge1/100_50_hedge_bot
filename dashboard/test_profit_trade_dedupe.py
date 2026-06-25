#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(DASHBOARD_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from app import _dedupe_profit_trade_rows_by_trade_block_id  # noqa: E402

TBID_A = "2b3e3f30-3224-4b6b-8be1-4118197d1d55"
TBID_B = "11111111-1111-1111-1111-111111111111"


class ProfitTradeDedupeTests(unittest.TestCase):
    def test_summary_and_process_same_trade_block_id_becomes_one_row(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "closed",
                "profit_usdt": 0.04,
                "confirmed_pnl_row_count": 1,
            },
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "in_progress",
                "is_process": True,
                "active_orders": [{"purpose": "CYCLE_2_LONG_ADD"}],
            },
        ]
        deduped = _dedupe_profit_trade_rows_by_trade_block_id(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["trade_block_id"], TBID_A)

    def test_closed_summary_preferred_over_process_row(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "in_progress",
                "is_process": True,
                "profit_usdt": None,
                "confirmed_pnl_row_count": 0,
            },
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "closed",
                "profit_usdt": 0.04107762,
                "confirmed_pnl_row_count": 1,
            },
        ]
        deduped = _dedupe_profit_trade_rows_by_trade_block_id(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["status"], "closed")
        self.assertEqual(deduped[0]["profit_usdt"], 0.04107762)
        self.assertEqual(deduped[0]["confirmed_pnl_row_count"], 1)

    def test_active_process_preferred_when_no_closed_summary(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "open",
                "profit_usdt": None,
                "confirmed_pnl_row_count": 0,
            },
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "in_progress",
                "is_process": True,
                "confirmed_pnl_row_count": 2,
                "filled_orders": [{"purpose": "CYCLE_1_LONG_REDUCE"}],
                "active_orders": [{"purpose": "CYCLE_2_LONG_ADD"}],
            },
        ]
        deduped = _dedupe_profit_trade_rows_by_trade_block_id(rows)
        self.assertEqual(len(deduped), 1)
        self.assertTrue(deduped[0].get("is_process"))
        self.assertEqual(deduped[0]["status"], "in_progress")
        self.assertEqual(deduped[0]["confirmed_pnl_row_count"], 2)
        self.assertEqual(len(deduped[0].get("filled_orders") or []), 1)
        self.assertEqual(len(deduped[0].get("active_orders") or []), 1)

    def test_confirmed_pnl_row_count_merged_from_other_candidate(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "closed",
                "profit_usdt": 0.04,
            },
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "closed",
                "confirmed_pnl_row_count": 3,
                "filled_orders": [{"purpose": "CYCLE_1_SHORT_REDUCE"}],
            },
        ]
        deduped = _dedupe_profit_trade_rows_by_trade_block_id(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["confirmed_pnl_row_count"], 3)
        self.assertEqual(deduped[0]["profit_usdt"], 0.04)
        self.assertEqual(len(deduped[0].get("filled_orders") or []), 1)

    def test_different_trade_block_ids_remain_separate(self) -> None:
        rows = [
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "status": "closed",
                "profit_usdt": 0.04,
            },
            {
                "trade_block_id": TBID_B,
                "bot_name": "long_bot_1",
                "status": "in_progress",
                "is_process": True,
            },
        ]
        deduped = _dedupe_profit_trade_rows_by_trade_block_id(rows)
        self.assertEqual(len(deduped), 2)
        tbids = {row["trade_block_id"] for row in deduped}
        self.assertEqual(tbids, {TBID_A, TBID_B})


if __name__ == "__main__":
    unittest.main()
