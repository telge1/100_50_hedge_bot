#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.confirmed_pnl_fill_context import (
    classify_exit_fill_for_audit,
    enrich_fill_for_confirmed_pnl,
    recover_purpose_from_client_order_id,
)
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    configure_confirmed_order_pnl_history_file,
    set_default_bot_name,
)
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, RuntimeState

TBID = "5a1a50e3-5537-4fb8-8683-ffd088bbf3c2"


def _snapshot() -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="APTUSDT",
        current_price=0.5947,
        long_qty=100.0,
        short_qty=38.21,
        long_avg=0.60,
        short_avg=0.595,
    )


def _context(order_manager: mock.Mock | None = None) -> StrategyContext:
    return StrategyContext(
        audit=mock.Mock(),
        runtime_name="test",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        order_manager=order_manager or mock.Mock(),
    )


class ConfirmedPnlFillContextTests(unittest.TestCase):
    def test_recovers_cycle_purpose_from_client_order_id(self) -> None:
        purpose = recover_purpose_from_client_order_id(
            "fixed_cycle-cycle_2_short_reduce-split0-abc123",
        )
        self.assertEqual(purpose, "CYCLE_2_SHORT_REDUCE")

    def test_classifies_short_reduce_without_metadata(self) -> None:
        fill_type, cycle_index = classify_exit_fill_for_audit(
            "CYCLE_2_SHORT_REDUCE",
            {},
        )
        self.assertEqual(fill_type, "cycle_short_tp")
        self.assertEqual(cycle_index, 2)


class CycleReduceConfirmedHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "confirmed_order_pnl_history.jsonl"
        configure_confirmed_order_pnl_history_file(self.history_path)
        set_default_bot_name("long_bot_1")
        self.strategy = FixedCycleHedgeStrategy()
        self.runtime_state = RuntimeState(
            strategy_state={
                "trade_block_id": TBID,
                "processed_cycle_purposes": ["CYCLE_1_LONG_ADD"],
                "cycle_state_entry": {},
            }
        )
        self.runtime_state.last_snapshot = _snapshot()

    def tearDown(self) -> None:
        configure_confirmed_order_pnl_history_file(None)
        set_default_bot_name("long_bot_1")
        self.temp_dir.cleanup()

    def _read_rows(self) -> list[dict]:
        if not self.history_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.history_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def test_empty_cycle_state_short_reduce_fill_writes_confirmed_history(self) -> None:
        fill_event = FillEvent(
            exchange_order_id="bybit-order-apt-1",
            client_order_id="fixed_cycle-cycle_2_short_reduce-split0-abc123",
            side="short",
            purpose="",
            exec_qty=38.21,
            exec_price=0.5947,
            order_type="Limit",
            reduce_only=True,
            status="FILLED",
            metadata={},
            occurred_at=datetime.now(timezone.utc),
        )
        order_manager = mock.Mock()
        order_manager.fetch_closed_pnl.return_value = [
            {
                "orderId": "bybit-order-apt-1",
                "symbol": "APTUSDT",
                "side": "Buy",
                "closedPnl": "0.4225",
                "qty": "38.21",
                "avgExitPrice": "0.5947",
                "updatedTime": "1710000000000",
            }
        ]
        context = _context(order_manager)

        self.strategy.on_fill(fill_event, _snapshot(), self.runtime_state, context)

        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["purpose"], "CYCLE_2_SHORT_REDUCE")
        self.assertEqual(row["cycle_index"], 2)
        self.assertEqual(row["trade_block_id"], TBID)
        self.assertAlmostEqual(float(row["closed_pnl"]), 0.4225, places=4)

    def test_processed_without_history_triggers_reconcile_warning(self) -> None:
        self.runtime_state.strategy_state["processed_cycle_purposes"] = [
            "CYCLE_1_LONG_ADD",
            "CYCLE_2_SHORT_REDUCE",
        ]
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event"
        ) as warning_mock:
            self.strategy._reconcile_processed_cycle_purposes_confirmed_history(
                self.runtime_state,
                _context(),
            )
            warning_names = [call.args[0] for call in warning_mock.call_args_list]
        self.assertIn("fixed_cycle_processed_purpose_missing_confirmed_history", warning_names)
        processed = self.runtime_state.strategy_state.get("processed_cycle_purposes") or []
        self.assertNotIn("CYCLE_2_SHORT_REDUCE", processed)

    def test_mark_processed_deferred_until_confirmed_history_exists(self) -> None:
        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_warning_event"
        ) as warning_mock:
            self.strategy._mark_cycle_purpose_status(
                self.runtime_state,
                purpose="CYCLE_2_SHORT_REDUCE",
                metadata={"cycle_index": 2, "cycle_role": "short_reduce"},
                status="FILLED",
            )
            warning_names = [call.args[0] for call in warning_mock.call_args_list]
        self.assertIn("fixed_cycle_cycle_purpose_deferred_until_confirmed_history", warning_names)
        processed = self.runtime_state.strategy_state.get("processed_cycle_purposes") or []
        self.assertNotIn("CYCLE_2_SHORT_REDUCE", processed)


class DashboardConfirmedRowTests(unittest.TestCase):
    def test_dashboard_loader_sees_cycle_reduce_row(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        history_path = Path(temp_dir.name) / "confirmed_order_pnl_history.jsonl"
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": "APTUSDT",
            "exchange_order_id": "bybit-order-apt-1",
            "client_order_id": "fixed_cycle-cycle_2_short_reduce-split0-abc123",
            "purpose": "CYCLE_2_SHORT_REDUCE",
            "closed_pnl": 0.4225,
            "trade_block_id": TBID,
            "cycle_index": 2,
            "pnl_scope": "cycle",
            "dedupe_key": "bybit-order-apt-1:CYCLE_2_SHORT_REDUCE",
            "source": "bot_confirmed_pnl",
            "bot_name": "long_bot_1",
        }
        history_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
        try:
            from fixed_cycle_hedge_bot.confirmed_pnl_path_logic import (
                load_valid_confirmed_pnl_rows_from_paths,
            )

            rows = load_valid_confirmed_pnl_rows_from_paths([history_path])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["purpose"], "CYCLE_2_SHORT_REDUCE")
            self.assertEqual(rows[0]["trade_block_id"], TBID)
        finally:
            temp_dir.cleanup()


class EnrichFillTests(unittest.TestCase):
    def test_enrich_sets_purpose_cycle_index_and_role(self) -> None:
        fill_event = FillEvent(
            exchange_order_id="order-1",
            client_order_id="fixed_cycle-cycle_2_short_reduce-xyz",
            side="short",
            purpose="",
            exec_qty=1.0,
            exec_price=1.0,
            order_type="Limit",
            reduce_only=True,
            status="FILLED",
        )
        enrich_fill_for_confirmed_pnl(fill_event)
        self.assertEqual(fill_event.purpose, "CYCLE_2_SHORT_REDUCE")
        self.assertEqual(fill_event.metadata.get("cycle_index"), 2)
        self.assertEqual(fill_event.metadata.get("cycle_role"), "short_reduce")


if __name__ == "__main__":
    unittest.main()
