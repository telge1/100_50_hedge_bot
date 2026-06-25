#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path

DASHBOARD_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = DASHBOARD_ROOT.parent
sys.path.insert(0, str(DASHBOARD_ROOT))
sys.path.insert(0, str(PROJECT_ROOT))

from app import (  # noqa: E402
    _apply_confirmed_end_profit_to_record,
    _build_trade_block_detail_rows,
    _dedupe_trade_block_detail_rows,
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


if __name__ == "__main__":
    unittest.main()
