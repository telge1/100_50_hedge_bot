#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

DASHBOARD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(DASHBOARD_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from app import (  # noqa: E402
    NON_FINAL_EXIT_CLOSE_WARNING_LABEL,
    _analyze_confirmed_final_exit,
    _apply_trade_block_close_status,
    _build_profit_trade_filtered_rows,
    _build_profit_trade_page,
    _build_trade_block_detail_rows,
    _is_final_exit_confirmed_row,
    _is_open_trade_for_summary,
    _profit_trade_stable_sort_key,
    _sort_profit_trade_rows_default,
    _sort_trade_block_detail_rows,
    _summarize_trade_blocks,
    _sum_confirmed_closed_pnl_rows,
    _verify_profit_trade_list_invariants,
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
        self.assertEqual(row["close_reason"], "no_final_exit_confirmed")
        self.assertEqual(row["warning_label"], NON_FINAL_EXIT_CLOSE_WARNING_LABEL)
        self.assertTrue(row["display_warning"])
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

    def test_later_start_time_sorts_before_earlier_even_if_end_time_is_later(self) -> None:
        rows = [
            {
                "trade_block_id": "e0e3bf29-0000-0000-0000-000000000001",
                "status": "closed",
                "start_time": "2026-06-23T13:33:44+00:00",
                "end_time": "2026-06-23T14:09:25+00:00",
            },
            {
                "trade_block_id": "06f0da6c-0000-0000-0000-000000000002",
                "status": "closed",
                "start_time": "2026-06-23T13:45:02+00:00",
                "end_time": "2026-06-23T13:45:02+00:00",
            },
        ]
        _sort_profit_trade_rows_default(rows)
        self.assertEqual(rows[0]["trade_block_id"], "06f0da6c-0000-0000-0000-000000000002")
        self.assertEqual(rows[1]["trade_block_id"], "e0e3bf29-0000-0000-0000-000000000001")

    def test_later_start_time_wins_over_later_end_time_on_previous_day(self) -> None:
        rows = [
            {
                "trade_block_id": "2feffbf9-0000-0000-0000-000000000001",
                "status": "closed",
                "start_time": "2026-06-22T09:44:12+00:00",
                "end_time": "2026-06-22T09:55:42+00:00",
            },
            {
                "trade_block_id": "2b69e7b3-0000-0000-0000-000000000002",
                "status": "closed",
                "start_time": "2026-06-22T09:52:08+00:00",
                "end_time": "2026-06-22T09:52:08+00:00",
            },
        ]
        _sort_profit_trade_rows_default(rows)
        self.assertEqual(rows[0]["trade_block_id"], "2b69e7b3-0000-0000-0000-000000000002")
        self.assertEqual(rows[1]["trade_block_id"], "2feffbf9-0000-0000-0000-000000000001")

    def test_closed_without_final_exit_sorted_by_start_time_not_end_time(self) -> None:
        index = {
            "start_times": {
                TBID_NEAR: datetime(2026, 6, 23, 13, 45, 2, tzinfo=timezone.utc),
                TBID_A: datetime(2026, 6, 23, 13, 33, 44, tzinfo=timezone.utc),
            },
            "end_times": {
                TBID_NEAR: datetime(2026, 6, 23, 13, 45, 2, tzinfo=timezone.utc),
                TBID_A: datetime(2026, 6, 23, 14, 9, 25, tzinfo=timezone.utc),
            },
        }
        rows = [
            {
                "trade_block_id": TBID_A,
                "status": "closed_without_final_exit",
                "no_final_exit_confirmed": True,
                "profit_is_final": False,
            },
            {
                "trade_block_id": TBID_NEAR,
                "status": "closed_without_final_exit",
                "no_final_exit_confirmed": True,
                "profit_is_final": False,
            },
        ]
        _sort_profit_trade_rows_default(rows, index)
        self.assertEqual(rows[0]["trade_block_id"], TBID_NEAR)
        self.assertEqual(rows[1]["trade_block_id"], TBID_A)
        self.assertEqual(_profit_trade_stable_sort_key(rows[0], index)[0], 1)
        self.assertEqual(_profit_trade_stable_sort_key(rows[1], index)[0], 1)

    def test_closed_without_final_exit_is_not_sorted_as_active(self) -> None:
        index = self._index()
        rows = [
            {
                "trade_block_id": TBID_NEAR,
                "status": "closed_without_final_exit",
                "no_final_exit_confirmed": True,
                "profit_is_final": False,
            },
            {
                "trade_block_id": TBID_A,
                "status": "in_progress",
                "is_process": True,
                "start_time": "2026-05-01T08:00:00+00:00",
            },
        ]
        _sort_profit_trade_rows_default(rows, index)
        self.assertEqual(rows[0]["trade_block_id"], TBID_A)
        self.assertEqual(rows[1]["trade_block_id"], TBID_NEAR)
        self.assertEqual(_profit_trade_stable_sort_key(rows[0], index)[0], 0)
        self.assertEqual(_profit_trade_stable_sort_key(rows[1], index)[0], 1)


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


class TradeBlockSummaryOpenCountTests(unittest.TestCase):
    def test_closed_without_final_exit_not_counted_as_open(self) -> None:
        trades = [
            {
                "trade_block_id": TBID_NEAR,
                "status": "closed_without_final_exit",
                "profit_usdt": -7.02,
                "no_final_exit_confirmed": True,
                "profit_is_final": False,
            },
            {
                "trade_block_id": TBID_A,
                "status": "in_progress",
                "is_process": True,
                "profit_usdt": None,
            },
        ]
        summary = _summarize_trade_blocks(trades)
        self.assertEqual(summary["open_trades"], 1)
        self.assertEqual(summary["closed_trades"], 1)
        self.assertEqual(summary["non_final_closed_trades"], 1)
        self.assertEqual(summary["final_closed_trades"], 0)
        self.assertFalse(_is_open_trade_for_summary(trades[0]))

    def test_only_real_active_process_rows_count_as_open(self) -> None:
        trades = [
            {"trade_block_id": TBID_A, "status": "closed", "profit_usdt": 1.0, "final_exit_confirmed": True, "profit_is_final": True},
            {"trade_block_id": TBID_B, "status": "closed_without_final_exit", "profit_usdt": -1.0, "no_final_exit_confirmed": True},
            {"trade_block_id": TBID_NEAR, "status": "in_progress", "is_process": True},
        ]
        summary = _summarize_trade_blocks(trades)
        self.assertEqual(summary["open_trades"], 1)
        self.assertEqual(summary["closed_trades"], 2)
        self.assertEqual(summary["final_closed_trades"], 1)
        self.assertEqual(summary["non_final_closed_trades"], 1)
        self.assertEqual(summary["final_winning_trades"], 1)
        self.assertEqual(summary["final_losing_trades"], 0)
        self.assertEqual(summary["winning_trades"], summary["final_winning_trades"])
        self.assertEqual(summary["winrate"], summary["final_winrate"])


class FilteredRowsStatusIntegrationTests(unittest.TestCase):
    def test_build_filtered_rows_marks_near_tbid_incomplete(self) -> None:
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
        self.assertEqual(match["close_reason"], "no_final_exit_confirmed")
        self.assertTrue(match["display_warning"])


class LongShortParityAndInvariantTests(unittest.TestCase):
    def _sample_rows(self) -> tuple[list[dict], dict]:
        confirmed_rows = _near_short_confirmed_rows()
        confirmed_index = {
            "by_trade_block_id": {TBID_NEAR: confirmed_rows},
            "start_times": {TBID_NEAR: datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)},
            "end_times": {TBID_NEAR: datetime(2026, 5, 10, 15, 0, tzinfo=timezone.utc)},
        }
        base_trades = [
            {
                "trade_block_id": TBID_NEAR,
                "bot_name": "short_bot_1",
                "symbol": "NEARUSDT",
                "status": "closed",
                "profit_usdt": -7.02,
                "cycle_count": 6,
            },
            {
                "trade_block_id": TBID_A,
                "bot_name": "long_bot_1",
                "symbol": "BTCUSDT",
                "status": "closed",
                "profit_usdt": 1.5,
                "final_exit_confirmed": True,
                "profit_is_final": True,
                "start_time": "2026-06-10T10:00:00+00:00",
                "end_time": "2026-06-10T12:00:00+00:00",
            },
        ]
        return base_trades, confirmed_index

    def _build_side_rows(self, bot_side: str) -> tuple[list[dict], dict[str, Any]]:
        from app import _summarize_trade_blocks

        base_trades, confirmed_index = self._sample_rows()
        with mock.patch(
            "app._load_trade_blocks_for_profile",
            return_value=(base_trades, [], {}),
        ):
            with mock.patch(
                "app._collect_active_bot_process_rows",
                return_value=([], []),
            ):
                with mock.patch(
                    "app._build_confirmed_pnl_index",
                    return_value=confirmed_index,
                ):
                    rows, _warnings, index = _build_profit_trade_filtered_rows(
                        "bot_1",
                        limit=50,
                        bot_side=bot_side,
                    )
        summary = _summarize_trade_blocks(rows)
        return rows, summary

    def test_long_and_short_use_same_sort_key_order(self) -> None:
        long_rows, _ = self._build_side_rows("long")
        short_rows, _ = self._build_side_rows("short")
        long_order = [row["trade_block_id"] for row in long_rows]
        short_order = [row["trade_block_id"] for row in short_rows]
        self.assertEqual(long_order, short_order)

    def test_verify_invariants_passes_for_long_and_short(self) -> None:
        for bot_side in ("long", "short"):
            rows, summary = self._build_side_rows(bot_side)
            errors = _verify_profit_trade_list_invariants(rows, summary, max_check=20)
            self.assertEqual(errors, [], msg=f"{bot_side}: {errors}")


TBID_FINAL = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


class FrontendStatusDisplayContractTests(unittest.TestCase):
    def test_profit_trades_js_renders_closed_without_final_exit_not_as_open(self) -> None:
        js_path = DASHBOARD_ROOT / "static" / "js" / "profit_trades.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn("function getTradeStatusLabel", content)
        self.assertIn("closed_without_final_exit", content)
        self.assertIn("Ohne Final Exit", content)
        self.assertIn("warning_label", content)
        self.assertIn("function getTradeStatusLabel(trade)", content)
        self.assertNotIn(
            'isClosed ? "Closed" : isProcess ? "In Progress" : "Open"',
            content,
        )


class FinalExitProcessSuppressionTests(unittest.TestCase):
    def test_converted_process_row_does_not_reopen_as_in_progress(self) -> None:
        confirmed_rows = [
            {
                "trade_block_id": TBID_FINAL,
                "timestamp": "2026-05-10T10:00:00+00:00",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "closed_pnl": -0.5,
                "pnl_scope": "cycle",
                "source": "bot_confirmed_pnl",
                "exchange_order_id": "cycle-order",
            },
            {
                "trade_block_id": TBID_FINAL,
                "timestamp": "2026-05-10T16:00:00+00:00",
                "purpose": "SHORT_SL_EXIT",
                "closed_pnl": 0.5,
                "pnl_scope": "final_exit",
                "source": "bot_confirmed_pnl",
                "exchange_order_id": "final-order",
            },
        ]
        confirmed_index = {
            "by_trade_block_id": {TBID_FINAL: confirmed_rows},
            "start_times": {TBID_FINAL: datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)},
            "end_times": {TBID_FINAL: datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc)},
        }
        process_row = {
            "trade_block_id": TBID_FINAL,
            "bot_name": "short_bot_1",
            "symbol": "NEARUSDT",
            "status": "in_progress",
            "is_process": True,
            "active_orders": [{"purpose": "CYCLE_7_SHORT_REDUCE"}],
        }
        with mock.patch(
            "app._load_trade_blocks_for_profile",
            return_value=([], [], {}),
        ):
            with mock.patch(
                "app._collect_active_bot_process_rows",
                return_value=([process_row], []),
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
        match = next(row for row in rows if row.get("trade_block_id") == TBID_FINAL)
        self.assertEqual(match["status"], "closed")
        self.assertFalse(match.get("is_process"))
        self.assertTrue(match.get("final_exit_confirmed"))
        self.assertTrue(match.get("profit_is_final"))


class LongShortStartTimeSortApiTests(unittest.TestCase):
    def _sorted_rows_for_side(self, bot_side: str) -> list[str]:
        rows = [
            {
                "trade_block_id": "e0e3bf29-0000-0000-0000-000000000001",
                "bot_name": f"{'long' if bot_side == 'long' else 'short'}_bot_1",
                "status": "closed",
                "start_time": "2026-06-23T13:33:44+00:00",
                "end_time": "2026-06-23T14:09:25+00:00",
            },
            {
                "trade_block_id": "06f0da6c-0000-0000-0000-000000000002",
                "bot_name": f"{'long' if bot_side == 'long' else 'short'}_bot_1",
                "status": "closed",
                "start_time": "2026-06-23T13:45:02+00:00",
                "end_time": "2026-06-23T13:45:02+00:00",
            },
        ]
        _sort_profit_trade_rows_default(rows)
        with mock.patch(
            "app._build_profit_trade_filtered_rows",
            return_value=(rows, [], {}),
        ):
            with mock.patch(
                "app._build_profit_trade_wallet_summary",
                return_value={},
            ):
                api_rows, _summary, _pagination, _warnings, _metadata = _build_profit_trade_page(
                    "bot_1",
                    limit=50,
                    page=0,
                    page_size=50,
                    bot_side=bot_side,
                )
        return [row["trade_block_id"] for row in api_rows]

    def test_long_api_sorted_by_start_time_desc(self) -> None:
        order = self._sorted_rows_for_side("long")
        self.assertEqual(
            order,
            [
                "06f0da6c-0000-0000-0000-000000000002",
                "e0e3bf29-0000-0000-0000-000000000001",
            ],
        )

    def test_short_api_sorted_by_start_time_desc(self) -> None:
        order = self._sorted_rows_for_side("short")
        self.assertEqual(
            order,
            [
                "06f0da6c-0000-0000-0000-000000000002",
                "e0e3bf29-0000-0000-0000-000000000001",
            ],
        )


class PaginationAfterFinalSortTests(unittest.TestCase):
    def test_pagination_slices_after_final_sort(self) -> None:
        rows = [
            {
                "trade_block_id": f"tbid-{idx}",
                "bot_name": "long_bot_1",
                "status": "closed",
                "start_time": f"2026-06-{10 - idx:02d}T10:00:00+00:00",
            }
            for idx in range(5)
        ]
        _sort_profit_trade_rows_default(rows)
        with mock.patch(
            "app._build_profit_trade_filtered_rows",
            return_value=(rows, [], {}),
        ):
            with mock.patch(
                "app._build_profit_trade_wallet_summary",
                return_value={},
            ):
                page0, _summary0, _pag0, _warnings0, _meta0 = _build_profit_trade_page(
                    "bot_1",
                    limit=50,
                    page=0,
                    page_size=2,
                    bot_side="long",
                )
                page1, _summary1, pag1, _warnings1, _meta1 = _build_profit_trade_page(
                    "bot_1",
                    limit=50,
                    page=1,
                    page_size=2,
                    bot_side="long",
                )
        self.assertEqual(page0[0]["trade_block_id"], rows[0]["trade_block_id"])
        self.assertEqual(page0[1]["trade_block_id"], rows[1]["trade_block_id"])
        self.assertEqual(page1[0]["trade_block_id"], rows[2]["trade_block_id"])
        self.assertEqual(page1[1]["trade_block_id"], rows[3]["trade_block_id"])
        self.assertEqual(pag1.get("page"), 1)


class ClosedWithoutFinalExitNotOpenTests(unittest.TestCase):
    def test_backend_and_frontend_contract_treat_as_inactive(self) -> None:
        trade = {
            "trade_block_id": TBID_NEAR,
            "status": "closed_without_final_exit",
            "no_final_exit_confirmed": True,
            "profit_is_final": False,
            "warning_label": NON_FINAL_EXIT_CLOSE_WARNING_LABEL,
        }
        self.assertFalse(_is_open_trade_for_summary(trade))
        self.assertEqual(_profit_trade_stable_sort_key(trade)[0], 1)

        js_path = DASHBOARD_ROOT / "static" / "js" / "profit_trades.js"
        content = js_path.read_text(encoding="utf-8")
        self.assertIn('status === "closed_without_final_exit"', content)


if __name__ == "__main__":
    unittest.main()
