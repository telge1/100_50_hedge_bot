#!/usr/bin/env python3
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    configure_confirmed_order_pnl_history_file,
    set_default_bot_name,
)
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, RuntimeState

TBID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _snapshot() -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="NEARUSDT",
        current_price=5.0,
        long_qty=10.0,
        short_qty=10.0,
        long_avg=5.0,
        short_avg=5.0,
    )


def _fill_event(
    *,
    purpose: str,
    order_id: str = "order-1",
    side: str = "long",
    exec_qty: float = 1.5,
    exec_price: float = 5.01,
    metadata: dict | None = None,
) -> FillEvent:
    return FillEvent(
        exchange_order_id=order_id,
        client_order_id=f"client-{order_id}",
        side=side,
        purpose=purpose,
        exec_qty=exec_qty,
        exec_price=exec_price,
        order_type="Market",
        reduce_only=False,
        status="FILLED",
        metadata=metadata or {"cycle_index": 1, "runtime_calculated_pnl": 9.99, "exec_pnl": 8.88},
    )


def _context() -> StrategyContext:
    return StrategyContext(
        audit=mock.Mock(),
        runtime_name="test",
        symbol="NEARUSDT",
        category="linear",
        min_order_value=5.0,
    )


class RefillDocumentationWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = Path(self.temp_dir.name) / "confirmed_order_pnl_history.jsonl"
        configure_confirmed_order_pnl_history_file(self.history_path)
        self.runtime_state = RuntimeState(
            strategy_state={
                "trade_block_id": TBID,
                "active_cycle_index": 1,
            }
        )
        self.runtime_state.last_snapshot = _snapshot()
        set_default_bot_name("short_bot_1")
        self.strategy = FixedCycleHedgeStrategy()
        self.state = self.runtime_state.strategy_state

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

    def test_refill_long_written_without_registry_entry(self) -> None:
        fill_event = _fill_event(purpose="REFILL_LONG", order_id="refill-long-1", side="long")
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event,
            _snapshot(),
            self.runtime_state,
            self.state,
            pnl_scope="refill",
            pnl_source="refill_fill",
            confirmed_via="refill_fill",
            order_source="cycle_refill",
        )
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "REFILL_LONG")
        self.assertEqual(rows[0]["closed_pnl"], 0.0)
        self.assertEqual(rows[0]["pnl_scope"], "refill")
        self.assertEqual(rows[0]["bot_name"], "short_bot_1")

    def test_refill_short_written_without_registry_entry(self) -> None:
        fill_event = _fill_event(purpose="REFILL_SHORT", order_id="refill-short-1", side="short")
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event,
            _snapshot(),
            self.runtime_state,
            self.state,
            pnl_scope="refill",
            pnl_source="refill_fill",
            confirmed_via="refill_fill",
            order_source="cycle_refill",
        )
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "REFILL_SHORT")
        self.assertEqual(rows[0]["closed_pnl"], 0.0)

    def test_refill_closed_pnl_forced_zero_despite_metadata_pnl(self) -> None:
        fill_event = _fill_event(
            purpose="REFILL_LONG",
            order_id="refill-pnl-guard",
            metadata={"runtime_calculated_pnl": 42.0, "exec_pnl": 33.0},
        )
        self.strategy._write_confirmed_order_pnl_history(
            {
                "exchange_order_id": fill_event.exchange_order_id,
                "purpose": fill_event.purpose,
                "closed_pnl": 99.0,
                "trade_block_id": TBID,
                "pnl_scope": "refill",
                "pnl_source": "refill_fill",
            },
            runtime_state=self.runtime_state,
        )
        rows = self._read_rows()
        self.assertEqual(rows[0]["closed_pnl"], 0.0)

    def test_recovery_reload_long_entry_written(self) -> None:
        fill_event = _fill_event(
            purpose="RECOVERY_RELOAD_LONG_ENTRY",
            order_id="recovery-long-1",
            side="long",
        )
        self.state["recovery_reload_id"] = "reload-123"
        self.state["recovery_required"] = True
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event,
            _snapshot(),
            self.runtime_state,
            self.state,
            pnl_scope="recovery_reload",
            pnl_source="recovery_reload_fill",
            confirmed_via="recovery_reload_fill",
            order_source="recovery_capital_reload",
            recovery_reload_id="reload-123",
        )
        rows = self._read_rows()
        self.assertEqual(rows[0]["purpose"], "RECOVERY_RELOAD_LONG_ENTRY")
        self.assertEqual(rows[0]["pnl_scope"], "recovery_reload")
        self.assertEqual(rows[0]["closed_pnl"], 0.0)
        self.assertEqual(rows[0]["recovery_reload_id"], "reload-123")

    def test_recovery_reload_short_entry_written(self) -> None:
        fill_event = _fill_event(
            purpose="RECOVERY_RELOAD_SHORT_ENTRY",
            order_id="recovery-short-1",
            side="short",
        )
        self.state["recovery_required"] = True
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event,
            _snapshot(),
            self.runtime_state,
            self.state,
            pnl_scope="recovery_reload",
            pnl_source="recovery_reload_fill",
            confirmed_via="recovery_reload_fill",
            order_source="recovery_capital_reload",
        )
        rows = self._read_rows()
        self.assertEqual(rows[0]["purpose"], "RECOVERY_RELOAD_SHORT_ENTRY")
        self.assertEqual(rows[0]["closed_pnl"], 0.0)

    def test_legacy_recovery_refill_purposes_supported(self) -> None:
        for purpose, side, order_id in (
            ("RECOVERY_REFILL_LONG", "long", "legacy-long"),
            ("RECOVERY_REFILL_SHORT", "short", "legacy-short"),
        ):
            with self.subTest(purpose=purpose):
                self.history_path.unlink(missing_ok=True)
                fill_event = _fill_event(purpose=purpose, order_id=order_id, side=side)
                self.state["recovery_required"] = True
                self.strategy._handle_recovery_refill_fill(
                    fill_event,
                    _snapshot(),
                    self.runtime_state,
                    _context(),
                )
                rows = self._read_rows()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["purpose"], purpose)
                self.assertEqual(rows[0]["closed_pnl"], 0.0)

    def test_short_owner_refill_long_writes_to_short_file(self) -> None:
        fill_event = _fill_event(purpose="REFILL_LONG", order_id="owner-short-long")
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event,
            _snapshot(),
            self.runtime_state,
            self.state,
            pnl_scope="refill",
            pnl_source="refill_fill",
            confirmed_via="refill_fill",
        )
        rows = self._read_rows()
        self.assertEqual(rows[0]["bot_name"], "short_bot_1")

    def test_long_owner_refill_short_writes_to_long_file(self) -> None:
        set_default_bot_name("long_bot_1")
        fill_event = _fill_event(purpose="REFILL_SHORT", order_id="owner-long-short", side="short")
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event,
            _snapshot(),
            self.runtime_state,
            self.state,
            pnl_scope="refill",
            pnl_source="refill_fill",
            confirmed_via="refill_fill",
        )
        rows = self._read_rows()
        self.assertEqual(rows[0]["bot_name"], "long_bot_1")

    def test_duplicate_exchange_order_id_and_purpose_not_written_twice(self) -> None:
        fill_event = _fill_event(purpose="REFILL_LONG", order_id="dup-order")
        kwargs = {
            "pnl_scope": "refill",
            "pnl_source": "refill_fill",
            "confirmed_via": "refill_fill",
        }
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event, _snapshot(), self.runtime_state, self.state, **kwargs
        )
        self.strategy._write_refill_documentation_confirmed_row(
            fill_event, _snapshot(), self.runtime_state, self.state, **kwargs
        )
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)

    def test_advance_cycle_from_fill_writes_refill_without_registry(self) -> None:
        fill_event = _fill_event(purpose="REFILL_LONG", order_id="advance-refill")
        context = _context()
        self.strategy._advance_cycle_from_fill(
            fill_event,
            self.runtime_state,
            context,
        )
        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["purpose"], "REFILL_LONG")
        self.assertEqual(rows[0]["qty"], 1.5)
        self.assertEqual(rows[0]["fill_price"], 5.01)


if __name__ == "__main__":
    unittest.main()
