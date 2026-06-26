#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

DASHBOARD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(DASHBOARD_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from app import (  # noqa: E402
    _apply_confirmed_end_profit_to_record,
    _append_paired_hedge_confirmed_paths,
    _build_confirmed_pnl_index,
    _build_confirmed_detail_rows,
    _build_trade_block_detail_rows,
    _dedupe_trade_block_detail_rows,
    _get_confirmed_pnl_rows_for_trade_block,
    _sort_trade_block_detail_rows,
    _sum_confirmed_closed_pnl_rows,
    _trade_block_detail_row_sort_key,
)

TBID = "2b3e3f30-3224-4b6b-8be1-4118197d1d55"


def _row(
    *,
    purpose: str,
    time: str | None = None,
    cycle_index: int | None = None,
    exchange_order_id: str | None = None,
    pnl_usdt: float | str = 0.0,
    detail_source: str = "confirmed",
) -> dict:
    return {
        "time": time,
        "purpose": purpose,
        "cycle_index": cycle_index,
        "exchange_order_id": exchange_order_id,
        "order_id": exchange_order_id,
        "pnl_usdt": pnl_usdt,
        "detail_source": detail_source,
        "record_source": "bot_confirmed_pnl" if detail_source == "confirmed" else None,
    }


class TradeBlockDetailSortTests(unittest.TestCase):
    def test_sorts_by_timestamp_ascending(self) -> None:
        rows = [
            _row(purpose="CYCLE_2_LONG_REDUCE", time="2026-06-03T12:00:00+00:00", exchange_order_id="b"),
            _row(purpose="CYCLE_1_LONG_REDUCE", time="2026-06-02T12:00:00+00:00", exchange_order_id="a"),
        ]
        sorted_rows = _sort_trade_block_detail_rows(rows)
        self.assertEqual(sorted_rows[0]["exchange_order_id"], "a")
        self.assertEqual(sorted_rows[1]["exchange_order_id"], "b")

    def test_cycle_one_before_cycle_two_at_same_timestamp(self) -> None:
        rows = [
            _row(
                purpose="CYCLE_2_LONG_REDUCE",
                time="2026-06-02T12:00:00+00:00",
                cycle_index=2,
                exchange_order_id="c2",
            ),
            _row(
                purpose="CYCLE_1_LONG_REDUCE",
                time="2026-06-02T12:00:00+00:00",
                cycle_index=1,
                exchange_order_id="c1",
            ),
        ]
        sorted_rows = _sort_trade_block_detail_rows(rows)
        self.assertEqual(sorted_rows[0]["exchange_order_id"], "c1")
        self.assertEqual(sorted_rows[1]["exchange_order_id"], "c2")

    def test_refill_stays_at_timestamp_position(self) -> None:
        rows = [
            _row(purpose="CYCLE_2_LONG_REDUCE", time="2026-06-04T12:00:00+00:00", exchange_order_id="c2"),
            _row(purpose="REFILL_LONG", time="2026-06-03T12:00:00+00:00", exchange_order_id="refill"),
            _row(purpose="CYCLE_1_LONG_REDUCE", time="2026-06-02T12:00:00+00:00", exchange_order_id="c1"),
        ]
        sorted_rows = _sort_trade_block_detail_rows(rows)
        self.assertEqual(
            [row["exchange_order_id"] for row in sorted_rows],
            ["c1", "refill", "c2"],
        )

    def test_final_exit_comes_last(self) -> None:
        rows = [
            _row(purpose="SHORT_SL_EXIT", time="2026-06-05T12:00:00+00:00", exchange_order_id="final"),
            _row(purpose="CYCLE_1_LONG_REDUCE", time="2026-06-02T12:00:00+00:00", exchange_order_id="c1"),
            _row(purpose="INITIAL_LONG_ENTRY", time="2026-06-01T12:00:00+00:00", exchange_order_id="entry"),
        ]
        sorted_rows = _sort_trade_block_detail_rows(rows)
        self.assertEqual(sorted_rows[-1]["exchange_order_id"], "final")
        self.assertEqual(sorted_rows[0]["exchange_order_id"], "entry")

    def test_rows_without_timestamp_sort_deterministically_after_timestamped_rows(self) -> None:
        with_ts = _row(purpose="CYCLE_1_LONG_REDUCE", time="2026-06-02T12:00:00+00:00", exchange_order_id="with-ts")
        without_ts_cycle = _row(purpose="CYCLE_1_SHORT_REDUCE", time=None, cycle_index=1, exchange_order_id="no-ts-1")
        without_ts_final = _row(purpose="LONG_TP_EXIT", time=None, exchange_order_id="no-ts-final")
        sorted_rows = _sort_trade_block_detail_rows([without_ts_final, without_ts_cycle, with_ts])
        self.assertEqual(sorted_rows[0]["exchange_order_id"], "with-ts")
        self.assertEqual(sorted_rows[1]["exchange_order_id"], "no-ts-1")
        self.assertEqual(sorted_rows[2]["exchange_order_id"], "no-ts-final")

    def test_initial_entry_before_cycles_when_timestamps_missing(self) -> None:
        rows = [
            _row(purpose="CYCLE_1_LONG_REDUCE", time=None, exchange_order_id="c1"),
            _row(purpose="INITIAL_LONG_ENTRY", time=None, exchange_order_id="entry"),
        ]
        sorted_rows = _sort_trade_block_detail_rows(rows)
        self.assertEqual(sorted_rows[0]["exchange_order_id"], "entry")
        self.assertEqual(sorted_rows[1]["exchange_order_id"], "c1")


class TradeBlockDetailDedupeTests(unittest.TestCase):
    def test_confirmed_row_preferred_over_filled_order_duplicate(self) -> None:
        rows = [
            _row(
                purpose="CYCLE_1_LONG_REDUCE",
                time="2026-06-02T12:00:00+00:00",
                exchange_order_id="same-id",
                pnl_usdt="-",
                detail_source="filled_order",
            ),
            _row(
                purpose="CYCLE_1_LONG_REDUCE",
                time="2026-06-02T12:00:00+00:00",
                exchange_order_id="same-id",
                pnl_usdt=0.42,
                detail_source="confirmed",
            ),
        ]
        deduped = _dedupe_trade_block_detail_rows(rows)
        self.assertEqual(len(deduped), 1)
        self.assertEqual(deduped[0]["detail_source"], "confirmed")
        self.assertEqual(deduped[0]["pnl_usdt"], 0.42)


class BuildTradeBlockDetailRowsTests(unittest.TestCase):
    def test_build_from_confirmed_rows_is_sorted(self) -> None:
        record = {"trade_block_id": TBID, "symbol": "NEARUSDT", "bot_name": "long_bot_1"}
        confirmed_rows = [
            {
                "trade_block_id": TBID,
                "timestamp": "2026-06-04T12:00:00+00:00",
                "purpose": "SHORT_SL_EXIT",
                "closed_pnl": 0.2,
                "exchange_order_id": "final",
                "pnl_scope": "final_exit",
                "source": "bot_confirmed_pnl",
            },
            {
                "trade_block_id": TBID,
                "timestamp": "2026-06-01T12:00:00+00:00",
                "purpose": "INITIAL_LONG_ENTRY",
                "closed_pnl": 0.0,
                "exchange_order_id": "entry",
                "pnl_scope": "entry",
                "source": "bot_confirmed_pnl",
            },
            {
                "trade_block_id": TBID,
                "timestamp": "2026-06-02T12:00:00+00:00",
                "purpose": "CYCLE_1_LONG_REDUCE",
                "closed_pnl": 0.1,
                "exchange_order_id": "c1",
                "cycle_index": 1,
                "pnl_scope": "cycle",
                "source": "bot_confirmed_pnl",
            },
        ]
        rows = _build_trade_block_detail_rows(record, confirmed_rows)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["purpose"], "INITIAL_LONG_ENTRY")
        self.assertEqual(rows[1]["purpose"], "CYCLE_1_LONG_REDUCE")
        self.assertEqual(rows[2]["purpose"], "SHORT_SL_EXIT")

    def test_endprofit_remains_sum_of_confirmed_rows(self) -> None:
        confirmed_rows = [
            {"trade_block_id": TBID, "closed_pnl": 0.5, "purpose": "CYCLE_1_LONG_REDUCE"},
            {"trade_block_id": TBID, "closed_pnl": -0.1, "purpose": "CYCLE_1_SHORT_REDUCE"},
            {"trade_block_id": TBID, "closed_pnl": 0.2, "purpose": "LONG_TP_EXIT"},
        ]
        record: dict = {"trade_block_id": TBID, "status": "closed"}
        end_profit, _count, _purposes, _purpose_counts, _scope_counts = _sum_confirmed_closed_pnl_rows(
            confirmed_rows
        )
        _apply_confirmed_end_profit_to_record(record, confirmed_rows)
        self.assertAlmostEqual(end_profit, 0.6)
        self.assertEqual(record["confirmed_pnl_row_count"], 3)
        detail_rows = _build_trade_block_detail_rows(record, confirmed_rows)
        self.assertEqual(len(detail_rows), 3)


class ShortPrimaryHedgeConfirmedRowsTests(unittest.TestCase):
    def _jto_confirmed_rows(self) -> list[dict]:
        tbid = "jto-tbid-0000-0000-0000-000000000001"
        return [
            {
                "trade_block_id": tbid,
                "bot_name": "short_bot_1",
                "symbol": "JTOUSDT",
                "timestamp": "2026-06-26T10:16:00+00:00",
                "purpose": "CYCLE_1_SHORT_REDUCE",
                "cycle_index": 1,
                "closed_pnl": -0.1,
                "pnl_scope": "cycle",
                "exchange_order_id": "short-c1",
                "dedupe_key": "short-c1:CYCLE_1_SHORT_REDUCE",
                "source": "bot_confirmed_pnl",
            },
            {
                "trade_block_id": tbid,
                "bot_name": "long_bot_1",
                "symbol": "JTOUSDT",
                "timestamp": "2026-06-26T10:17:00+00:00",
                "purpose": "CYCLE_1_LONG_REDUCE",
                "cycle_index": 1,
                "closed_pnl": 0.05,
                "pnl_scope": "cycle",
                "exchange_order_id": "long-c1",
                "dedupe_key": "long-c1:CYCLE_1_LONG_REDUCE",
                "source": "bot_confirmed_pnl",
            },
            {
                "trade_block_id": tbid,
                "bot_name": "short_bot_1",
                "symbol": "JTOUSDT",
                "timestamp": "2026-06-26T10:18:00+00:00",
                "purpose": "CYCLE_2_SHORT_REDUCE",
                "cycle_index": 2,
                "closed_pnl": -0.2,
                "pnl_scope": "cycle",
                "exchange_order_id": "short-c2",
                "dedupe_key": "short-c2:CYCLE_2_SHORT_REDUCE",
                "source": "bot_confirmed_pnl",
            },
            {
                "trade_block_id": tbid,
                "bot_name": "long_bot_1",
                "symbol": "JTOUSDT",
                "timestamp": "2026-06-26T10:21:00+00:00",
                "purpose": "LONG_SL_EXIT",
                "closed_pnl": -0.3,
                "pnl_scope": "final_exit",
                "exchange_order_id": "long-sl",
                "dedupe_key": "long-sl:LONG_SL_EXIT",
                "source": "bot_confirmed_pnl",
            },
            {
                "trade_block_id": tbid,
                "bot_name": "short_bot_1",
                "symbol": "JTOUSDT",
                "timestamp": "2026-06-26T10:22:00+00:00",
                "purpose": "SHORT_TP_EXIT",
                "closed_pnl": 0.4,
                "pnl_scope": "final_exit",
                "exchange_order_id": "short-tp",
                "dedupe_key": "short-tp:SHORT_TP_EXIT",
                "source": "bot_confirmed_pnl",
            },
        ]

    def test_short_primary_details_include_both_cycle_legs_and_final_exits(self) -> None:
        tbid = "jto-tbid-0000-0000-0000-000000000001"
        confirmed_rows = self._jto_confirmed_rows()
        record = {
            "trade_block_id": tbid,
            "symbol": "JTOUSDT",
            "bot_name": "short_bot_1",
            "status": "closed",
        }
        detail_rows = _build_trade_block_detail_rows(record, confirmed_rows)
        purposes = {row["purpose"] for row in detail_rows}
        self.assertEqual(len(detail_rows), 5)
        self.assertIn("CYCLE_1_SHORT_REDUCE", purposes)
        self.assertIn("CYCLE_1_LONG_REDUCE", purposes)
        self.assertIn("CYCLE_2_SHORT_REDUCE", purposes)
        self.assertIn("LONG_SL_EXIT", purposes)
        self.assertIn("SHORT_TP_EXIT", purposes)

    def test_dedupe_does_not_remove_cycle_long_reduce(self) -> None:
        confirmed_rows = self._jto_confirmed_rows()
        built = _build_confirmed_detail_rows(
            {"trade_block_id": confirmed_rows[0]["trade_block_id"], "bot_name": "short_bot_1"},
            confirmed_rows,
        )
        deduped = _dedupe_trade_block_detail_rows(built)
        purposes = {row["purpose"] for row in deduped}
        self.assertIn("CYCLE_1_LONG_REDUCE", purposes)
        self.assertEqual(len(deduped), 5)

    def test_summary_endprofit_equals_sum_of_all_five_confirmed_rows(self) -> None:
        confirmed_rows = self._jto_confirmed_rows()
        record: dict = {
            "trade_block_id": confirmed_rows[0]["trade_block_id"],
            "status": "closed",
        }
        end_profit, pnl_count, _purposes, _pc, _sc = _sum_confirmed_closed_pnl_rows(confirmed_rows)
        _apply_confirmed_end_profit_to_record(record, confirmed_rows)
        detail_rows = _build_trade_block_detail_rows(record, confirmed_rows)
        self.assertEqual(pnl_count, 5)
        self.assertEqual(len(detail_rows), 5)
        self.assertAlmostEqual(end_profit, -0.15)
        self.assertAlmostEqual(record["profit_usdt"], end_profit)

    def test_confirmed_index_merges_paired_hedge_paths_for_short_view(self) -> None:
        tbid = "jto-tbid-0000-0000-0000-000000000001"
        short_path = Path("/tmp/short_bot_1/logs/confirmed_order_pnl_history.jsonl")
        long_path = Path("/tmp/long_bot_1/logs/confirmed_order_pnl_history.jsonl")
        all_rows = self._jto_confirmed_rows()

        def _collect_side_effect(paths: list[Path]) -> list[dict]:
            collected: list[dict] = []
            for path in paths:
                path_str = str(path)
                if "short_bot_1" in path_str:
                    collected.extend(row for row in all_rows if row["bot_name"] == "short_bot_1")
                if "long_bot_1" in path_str:
                    collected.extend(row for row in all_rows if row["bot_name"] == "long_bot_1")
            return collected

        paired_source = {
            "confirmed_order_pnl_history_file": long_path,
            "bot_name": "long_bot_1",
        }
        paths: list[Path] = [short_path]
        with mock.patch("app._resolve_profit_trade_source", return_value=(paired_source, [])):
            _append_paired_hedge_confirmed_paths("bot_1", "short", paths)
        self.assertEqual(paths, [short_path, long_path])

        with mock.patch(
            "app._collect_confirmed_order_pnl_rows_from_paths",
            side_effect=_collect_side_effect,
        ):
            with mock.patch(
                "app._collect_confirmed_history_paths",
                return_value=[short_path, long_path],
            ):
                index = _build_confirmed_pnl_index("bot_1", "short")
        rows = _get_confirmed_pnl_rows_for_trade_block("", tbid, confirmed_index=index)
        purposes = {row["purpose"] for row in rows}
        self.assertEqual(len(rows), 5)
        self.assertIn("CYCLE_1_LONG_REDUCE", purposes)


if __name__ == "__main__":
    unittest.main()
