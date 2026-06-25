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
    _analyze_confirmed_final_exit,
    _apply_trade_block_close_status,
    _build_trade_block_detail_rows,
    _is_final_exit_confirmed_row,
    _profit_trade_stable_sort_key,
    _sort_profit_trade_rows_default,
    _sort_trade_block_detail_rows,
    _sum_confirmed_closed_pnl_rows,
)

TBID_NEAR = "3a9f3a16-868c-4a6f-b727-d1f4193f5610"
TBID_A = "11111111-1111-1111-1111-111111111111"
TBID_B = "22222222-2222-2222-2222-222222222222"


def _near_short_confirmed_rows() -> list[dict]:
    rows = [
        ("2026-05-10T10:00:00+00:00", "CYCLE_1_SHORT_REDUCE", 1, -0.5),
        ("2026-05-10T11:00:00+00:00", "CYCLE_2_SHORT_REDUCE", 2, -0.8),
        ("2026-05-10T11:30:00+00:00", "REFILL_SHORT", 2, 0.0),
        ("2026-05-10T12:00:00+00:00", "CYCLE_3_SHORT_REDUCE", 3, -1.0),
        ("2026-05-10T13:00:00+00:00", "CYCLE_4_SHORT_REDUCE", 4, -1.2),
        ("2026-05-10T13:30:00+00:00", "REFILL_SHORT", 4, 0.0),
        ("2026-05-10T14:00:00+00:00", "CYCLE_5_SHORT_REDUCE", 5, -1.5),
        ("2026-05-10T15:00:00+00:00", "CYCLE_6_SHORT_REDUCE", 6, -2.02),
    ]
    return [
        {
            "trade_block_id": TBID_NEAR,
            "timestamp": ts,
            "purpose": purpose,
            "cycle_index": cycle_index,
            "closed_pnl": pnl,
            "pnl_scope": "cycle" if purpose.startswith("CYCLE_") else "refill",
            "source": "bot_confirmed_pnl",
            "exchange_order_id": f"order-{idx}",
        }
        for idx, (ts, purpose, cycle_index, pnl) in enumerate(rows)
    ]


class FinalExitDetectionTests(unittest.TestCase):
    def test_cycle_and_refill_only_is_not_final_exit(self) -> None:
        meta = _analyze_confirmed_final_exit(_near_short_confirmed_rows())
        self.assertFalse(meta["final_exit_confirmed"])
        self.assertFalse(_is_final_exit_confirmed_row({"purpose": "CYCLE_6_SHORT_REDUCE", "pnl_scope": "cycle"}))
        self.assertFalse(_is_final_exit_confirmed_row({"purpose": "REFILL_SHORT", "pnl_scope": "refill"}))

    def test_short_sl_exit_with_final_scope_is_final_exit(self) -> None:
        self.assertTrue(
            _is_final_exit_confirmed_row({"purpose": "SHORT_SL_EXIT", "pnl_scope": "final_exit"})
        )


class TradeBlockStatusTests(unittest.TestCase):
    def test_near_tbid_without_final_exit_becomes_closed_without_final_exit(self) -> None:
        confirmed_rows = _near_short_confirmed_rows()
        row = {
            "trade_block_id": TBID_NEAR,
            "symbol": "NEARUSDT",
            "bot_name": "short_bot_1",
            "status": "closed",
            "profit_usdt": -7.02041579,
            "confirmed_pnl_row_count": 8,
            "cycle_count": 6,
        }
        meta = _analyze_confirmed_final_exit(confirmed_rows)
        confirmed_index = {
            "start_times": {
                TBID_NEAR: datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
            },
            "end_times": {
                TBID_NEAR: datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc),
            },
        }
        _apply_trade_block_close_status(
            row,
            confirmed_rows,
            meta,
            is_active_process=False,
            confirmed_index=confirmed_index,
        )
        self.assertEqual(row["status"], "closed_without_final_exit")
        self.assertTrue(row["no_final_exit_confirmed"])
        self.assertFalse(row["final_exit_confirmed"])
        self.assertFalse(row["profit_is_final"])
        end_profit, _count, _purposes, _pc, _sc = _sum_confirmed_closed_pnl_rows(confirmed_rows)
        self.assertAlmostEqual(end_profit, -7.02, places=2)

    def test_final_exit_confirmed_sets_normal_closed(self) -> None:
        confirmed_rows = _near_short_confirmed_rows() + [
            {
                "trade_block_id": TBID_NEAR,
                "timestamp": "2026-05-10T16:00:00+00:00",
                "purpose": "SHORT_SL_EXIT",
                "closed_pnl": 0.5,
                "pnl_scope": "final_exit",
                "source": "bot_confirmed_pnl",
            }
        ]
        row = {"trade_block_id": TBID_NEAR, "status": "open"}
        meta = _analyze_confirmed_final_exit(confirmed_rows)
        _apply_trade_block_close_status(row, confirmed_rows, meta, is_active_process=False)
        self.assertEqual(row["status"], "closed")
        self.assertTrue(row["final_exit_confirmed"])
        self.assertTrue(row["profit_is_final"])


class TradeBlockStableSortTests(unittest.TestCase):
    def _index(self) -> dict:
        return {
            "start_times": {
                TBID_A: datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc),
                TBID_B: datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc),
            },
            "end_times": {
                TBID_A: datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
                TBID_B: datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
            },
        }

    def test_same_index_sort_order_is_identical_for_long_and_short_views(self) -> None:
        rows = [
            {"trade_block_id": TBID_A, "status": "closed_without_final_exit"},
            {"trade_block_id": TBID_B, "status": "closed"},
        ]
        index = self._index()
        long_sorted = [row["trade_block_id"] for row in sorted(rows, key=lambda r: _profit_trade_stable_sort_key(r, index))]
        short_sorted = [row["trade_block_id"] for row in sorted(rows, key=lambda r: _profit_trade_stable_sort_key(r, index))]
        self.assertEqual(long_sorted, short_sorted)
        self.assertEqual(long_sorted, [TBID_B, TBID_A])

    def test_list_sort_uses_start_end_not_uuid(self) -> None:
        rows = [
            {"trade_block_id": "zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz", "status": "closed", "start_time": "2026-01-01T00:00:00+00:00"},
            {"trade_block_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "status": "closed", "start_time": "2026-06-01T00:00:00+00:00"},
        ]
        _sort_profit_trade_rows_default(rows)
        self.assertEqual(rows[0]["trade_block_id"], "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


class NearTbidDetailSortTests(unittest.TestCase):
    def test_detail_rows_sorted_by_timestamp_and_cycle(self) -> None:
        record = {"trade_block_id": TBID_NEAR, "symbol": "NEARUSDT", "bot_name": "short_bot_1"}
        rows = _build_trade_block_detail_rows(record, _near_short_confirmed_rows())
        self.assertEqual(len(rows), 8)
        purposes = [row["purpose"] for row in rows]
        self.assertEqual(purposes[0], "CYCLE_1_SHORT_REDUCE")
        self.assertEqual(purposes[-1], "CYCLE_6_SHORT_REDUCE")
        self.assertIn("REFILL_SHORT", purposes)
        timestamps = [row["time"] for row in rows]
        self.assertEqual(timestamps, sorted(timestamps))

    def test_final_exit_row_comes_last_when_present(self) -> None:
        confirmed = _near_short_confirmed_rows() + [
            {
                "trade_block_id": TBID_NEAR,
                "timestamp": "2026-05-10T16:00:00+00:00",
                "purpose": "SHORT_SL_EXIT",
                "closed_pnl": 0.1,
                "pnl_scope": "final_exit",
                "source": "bot_confirmed_pnl",
                "exchange_order_id": "final-order",
            }
        ]
        rows = _sort_trade_block_detail_rows(
            _build_trade_block_detail_rows({"trade_block_id": TBID_NEAR}, confirmed)
        )
        self.assertEqual(rows[-1]["purpose"], "SHORT_SL_EXIT")


class FilteredRowsStatusIntegrationTests(unittest.TestCase):
    def test_build_filtered_rows_marks_near_tbid_incomplete(self) -> None:
        from app import _build_profit_trade_filtered_rows

        confirmed_rows = _near_short_confirmed_rows()
        confirmed_index = {
            "by_trade_block_id": {TBID_NEAR: confirmed_rows},
            "start_times": {TBID_NEAR: datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)},
            "end_times": {TBID_NEAR: datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc)},
        }
        base_trade = {
            "trade_block_id": TBID_NEAR,
            "bot_name": "short_bot_1",
            "symbol": "NEARUSDT",
            "status": "closed",
            "profit_usdt": -7.02,
            "cycle_count": 6,
        }
        with mock.patch(
            "app._load_trade_blocks_for_profile",
            return_value=([base_trade], [], {}),
        ):
            with mock.patch(
                "app._collect_active_bot_process_rows",
                return_value=([], []),
            ):
                with mock.patch(
                    "app._build_confirmed_pnl_index",
                    return_value=confirmed_index,
                ):
                    rows, _warnings, _index = _build_profit_trade_filtered_rows(
                        "bot_1",
                        limit=50,
                        bot_side="short",
                    )
        match = next(row for row in rows if row.get("trade_block_id") == TBID_NEAR)
        self.assertEqual(match["status"], "closed_without_final_exit")
        self.assertTrue(match["no_final_exit_confirmed"])
        self.assertEqual(match["confirmed_pnl_row_count"], 8)


if __name__ == "__main__":
    unittest.main()
