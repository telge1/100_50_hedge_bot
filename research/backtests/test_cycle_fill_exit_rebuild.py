"""Backtest integration tests for exit rebuild after cycle fills."""

from __future__ import annotations

import unittest
from pathlib import Path

from fixed_cycle_hedge_bot.models import StrategyIntent

from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol, symbol_to_feather_name
from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_execution import process_candle_fills
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle
from research.backtests.trade_block_export import build_trade_block_rows, write_trade_block_exports


def _active_purposes(sim: HedgeBotOriginalSimulator) -> set[str]:
    return {
        str(order.purpose or "").upper()
        for order in sim.book.active_orders()
    }


def _rows_after_fill(rows: list[dict], *, fill_purpose_substr: str) -> list[dict]:
    fill_rows = [
        row
        for row in rows
        if row.get("row_type") == "fill" and fill_purpose_substr in str(row.get("purpose") or "")
    ]
    if not fill_rows:
        return []
    fill_time = fill_rows[0].get("timestamp")
    return [
        row
        for row in rows
        if row.get("timestamp") and row.get("timestamp") >= fill_time
    ]


def _exit_rebuild_purposes_after_fill(rows: list[dict], *, fill_purpose_substr: str) -> set[str]:
    """Collect exit rebuild signals from export rows after a cycle fill."""
    after = _rows_after_fill(rows, fill_purpose_substr=fill_purpose_substr)
    purposes: set[str] = set()
    for row in after:
        purpose = str(row.get("purpose") or "")
        if purpose not in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}:
            continue
        row_type = str(row.get("row_type") or "")
        event_type = str(row.get("event_type") or "")
        if row_type == "intent" and event_type == "after_fill":
            purposes.add(purpose)
        elif row_type == "order" and event_type in {"submitted", "cancelled"}:
            purposes.add(purpose)
    return purposes


def _assert_exit_rebuild_after_cycle_fill(
    test_case: unittest.TestCase,
    result,
    *,
    fill_purpose_substr: str,
    expect_cycle_follow_up: str | None = None,
) -> None:
    rows = build_trade_block_rows(result)
    after = _rows_after_fill(rows, fill_purpose_substr=fill_purpose_substr)
    test_case.assertTrue(after, f"missing fill rows for {fill_purpose_substr}")

    exit_purposes = _exit_rebuild_purposes_after_fill(
        rows, fill_purpose_substr=fill_purpose_substr
    )
    test_case.assertIn("LONG_TP_EXIT", exit_purposes)
    test_case.assertIn("SHORT_SL_EXIT", exit_purposes)

    if expect_cycle_follow_up:
        follow_up_rows = [
            row
            for row in after
            if expect_cycle_follow_up in str(row.get("purpose") or "")
            and (
                str(row.get("event_type") or "") in {"submitted", "after_fill"}
                or str(row.get("row_type") or "") == "intent"
            )
        ]
        test_case.assertTrue(
            follow_up_rows,
            f"missing follow-up rows for {expect_cycle_follow_up}",
        )


@unittest.skipUnless(
    (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    "APTUSDT candle data unavailable",
)
class CycleFillExitRebuildBacktestTests(unittest.TestCase):
    def _run(self, *, start_index: int, max_candles: int):
        candles = load_candles_for_symbol("APTUSDT", limit=50000)
        window = candles[start_index : start_index + max_candles + 1]
        return run_historical_backtest(
            "APTUSDT",
            "long",
            window,
            max_candles=max(0, len(window) - 1),
            config_source="live",
        )

    def test_after_cycle_1_long_add_exits_are_active(self) -> None:
        result = self._run(start_index=250, max_candles=1000)
        _assert_exit_rebuild_after_cycle_fill(
            self,
            result,
            fill_purpose_substr="CYCLE_1_LONG_ADD",
            expect_cycle_follow_up="CYCLE_1_SHORT_REDUCE",
        )

    def test_after_cycle_2_long_add_exits_are_active(self) -> None:
        result = self._run(start_index=250, max_candles=1000)
        _assert_exit_rebuild_after_cycle_fill(
            self,
            result,
            fill_purpose_substr="CYCLE_2_LONG_ADD",
            expect_cycle_follow_up="CYCLE_2_SHORT_REDUCE",
        )

    def test_short_reduce_and_exits_can_coexist(self) -> None:
        result = self._run(start_index=250, max_candles=1000)
        active_rows = [
            row
            for row in build_trade_block_rows(result)
            if row.get("row_type") == "final_active_order"
        ]
        combined = {
            str(row.get("purpose") or "")
            for row in active_rows
            if row.get("purpose")
        }
        if "CYCLE_2_SHORT_REDUCE" in combined:
            self.assertIn("LONG_TP_EXIT", combined)
            self.assertIn("SHORT_SL_EXIT", combined)

    def test_exit_fills_even_when_short_reduce_resting(self) -> None:
        config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=1.30,
            config_load=config_load,
        )
        try:
            sim.book.long_qty = 50.0
            sim.book.long_avg = 1.25
            sim.book.short_qty = 25.0
            sim.book.short_avg = 1.25
            sim.book.submit_intent(
                StrategyIntent(
                    side="short",
                    qty=4.0,
                    purpose="CYCLE_2_SHORT_REDUCE",
                    order_type="Market",
                    trigger_price=1.10,
                    trigger_direction=2,
                    reduce_only=True,
                )
            )
            sim.book.submit_intent(
                StrategyIntent(
                    side="long",
                    qty=50.0,
                    purpose="LONG_TP_EXIT",
                    order_type="Market",
                    trigger_price=1.28,
                    trigger_direction=1,
                    reduce_only=True,
                )
            )
            sim.book.submit_intent(
                StrategyIntent(
                    side="short",
                    qty=25.0,
                    purpose="SHORT_SL_EXIT",
                    order_type="Market",
                    trigger_price=1.28,
                    trigger_direction=1,
                    reduce_only=True,
                )
            )
            candle = SyntheticCandle(
                symbol="APTUSDT",
                open=1.25,
                high=1.31,
                low=1.24,
                close=1.30,
            )
            fills, _ = process_candle_fills(
                book=sim.book,
                runtime_state=sim.runtime_state,
                candle=candle,
                eligible_orders=list(sim.book.active_orders()),
                fill_model="conservative",
            )
            purposes = {fill.purpose for fill in fills}
            self.assertIn("LONG_TP_EXIT", purposes)
            self.assertIn("SHORT_SL_EXIT", purposes)
        finally:
            sim.close()

    def test_baseline_pnl_is_stable_between_runs(self) -> None:
        first = self._run(start_index=250, max_candles=1000)
        second = self._run(start_index=250, max_candles=1000)
        self.assertEqual(first.final_status, second.final_status)
        self.assertEqual(first.realized_pnl, second.realized_pnl)
        self.assertEqual(first.fills_count, second.fills_count)
        self.assertEqual(first.orders_submitted, second.orders_submitted)

    def test_start_index_8000_trade_block_export(self) -> None:
        result = self._run(start_index=8000, max_candles=15000)
        output_dir = Path("research/backtests/results/cycle_fill_exit_rebuild")
        output_dir.mkdir(parents=True, exist_ok=True)
        written = write_trade_block_exports(result, output_dir)
        self.assertTrue(written)

        rows = build_trade_block_rows(result)
        cycle_fill_rows = [
            row
            for row in rows
            if row.get("row_type") == "fill" and "CYCLE_" in str(row.get("purpose") or "")
        ]
        self.assertTrue(cycle_fill_rows)

        fill_purpose = str(cycle_fill_rows[0].get("purpose") or "")
        _assert_exit_rebuild_after_cycle_fill(self, result, fill_purpose_substr=fill_purpose)

        exit_submit_rows = [
            row
            for row in rows
            if row.get("row_type") == "order"
            and row.get("event_type") == "submitted"
            and str(row.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        ]
        self.assertTrue(exit_submit_rows)


if __name__ == "__main__":
    unittest.main()
