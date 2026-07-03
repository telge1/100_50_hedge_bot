#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, RuntimeState


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_cycle_pnl_ledger_and_vwap_repair")),
        runtime_name="test_runtime",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        order_manager=mock.Mock(),
        cancel_open_orders_by_purpose=mock.Mock(),
    )


def _strategy() -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="long_bot_1",
            strategy_side="long",
            symbol="APTUSDT",
            price_tick_size=0.0001,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.125,
            order_fee_rate_pct=0.055,
        )
    )


class RecomputeCyclePnlLedgerTotalsTests(unittest.TestCase):
    def test_recompute_cycle_pnl_ledger_totals_updates_aggregates(self) -> None:
        strategy = _strategy()
        ledger = {
            "cycle_pnl_entries": {
                "cycle_long_reduce:1:order-a": {"pnl": -0.5, "source": "confirmed", "is_confirmed": True},
                "cycle_short_tp:1:order-b": {"pnl": 0.3, "source": "confirmed", "is_confirmed": True},
            },
            "final_long_exit_pnl": 0.1,
            "final_short_exit_pnl": None,
        }
        strategy._recompute_cycle_pnl_ledger_totals(ledger)
        self.assertEqual(ledger["cycle_long_reduce_pnl"], {"1": -0.5})
        self.assertEqual(ledger["cycle_short_tp_pnl"], {"1": 0.3})
        self.assertAlmostEqual(ledger["total_realized_pnl"], -0.1)

    def test_record_cycle_pnl_entry_calls_recompute_without_attribute_error(self) -> None:
        strategy = _strategy()
        ledger: dict = {"cycle_pnl_entries": {}}
        fill_event = FillEvent(
            exchange_order_id="ex-1",
            client_order_id="cli-1",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=10.0,
            exec_price=0.6,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
        )
        strategy._record_cycle_pnl_entry(
            ledger,
            fill_type="cycle_short_tp",
            cycle_index=1,
            fill_event=fill_event,
            pnl=0.25,
            pnl_source="confirmed_closed_pnl",
        )
        self.assertAlmostEqual(ledger["cycle_short_tp_pnl"]["1"], 0.25)
        self.assertAlmostEqual(ledger["total_realized_pnl"], 0.25)


class RepairVwapFillEntryTests(unittest.TestCase):
    def test_repair_vwap_fill_entry_fixes_doubled_total_qty(self) -> None:
        entry = {
            "price": 0.29875,
            "qty": 20.664,
            "total_qty": 41.328,
            "weighted_price_sum": 12.346740000000002,
            "avg_price": 0.29875,
        }
        repaired = FixedCycleHedgeStrategy._repair_vwap_fill_entry(entry)
        self.assertAlmostEqual(repaired["total_qty"], 20.664)
        self.assertAlmostEqual(repaired["avg_price"], 0.5975)
        self.assertAlmostEqual(repaired["price"], 0.5975)


class AdvanceCycleFromFillVwapRepairTests(unittest.TestCase):
    def test_terminal_short_reduce_does_not_double_count_vwap(self) -> None:
        strategy = _strategy()
        runtime = RuntimeState(
            strategy_state={
                "trade_block_id": "tb-1",
                "active_cycle_index": 1,
                "cycle_step": "waiting_for_pair_second_leg",
                "processed_cycle_purposes": [],
            },
            last_snapshot=HedgeSnapshot(
                symbol="APTUSDT",
                current_price=0.6,
                long_qty=100.0,
                short_qty=50.0,
                long_avg=0.61,
                short_avg=0.61,
            ),
        )
        cycle_state = strategy._ensure_cycle_state(runtime)
        cycle_state["short_fills"] = {
            "1": {
                "price": 0.5975,
                "qty": 20.664,
                "total_qty": 20.664,
                "weighted_price_sum": 12.34674,
                "avg_price": 0.5975,
            }
        }
        seq_entry = strategy._get_cycle_sequence_entry(runtime, 1)
        seq_entry["short_reduce_fill_price"] = 0.5975
        seq_entry["short_reduce_fill_confirmed"] = True
        seq_entry["short_reduce_status"] = "FILLED"

        fill_event = FillEvent(
            exchange_order_id="ex-short-reduce",
            client_order_id="cli-short-reduce",
            side="short",
            purpose="CYCLE_1_SHORT_REDUCE",
            exec_qty=20.664,
            exec_price=0.5975,
            order_type="Market",
            reduce_only=True,
            status="FILLED",
            metadata={"cycle_index": 1, "cycle_role": "short_reduce"},
        )

        strategy._advance_cycle_from_fill(fill_event, runtime, _context())

        short_fill = cycle_state["short_fills"]["1"]
        self.assertAlmostEqual(short_fill["total_qty"], 20.664)
        self.assertAlmostEqual(short_fill["avg_price"], 0.5975)
        self.assertAlmostEqual(float(seq_entry["short_reduce_fill_price"]), 0.5975)


if __name__ == "__main__":
    unittest.main()
