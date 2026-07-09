#!/usr/bin/env python3
from __future__ import annotations

import math
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import (
    STEP_WAITING_FOR_PAIR_FIRST_LEG,
    STEP_WAITING_FOR_PAIR_SECOND_LEG,
)
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, RuntimeState


def _context() -> StrategyContext:
    order_manager = mock.Mock()
    order_manager.fetch_closed_pnl.return_value = []
    return StrategyContext(
        audit=mock.Mock(),
        runtime_name="test_short_first_leg_pnl",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        order_manager=order_manager,
    )


def _short_strategy() -> ShortFixedCycleHedgeStrategy:
    return ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            symbol="APTUSDT",
            base_notional_usdt=100.0,
            hedge_ratio_short=0.5,
            reduction_pct_per_fill=25,
            target_profit_usdt=0.25,
            long_fill_distance_pct=0.15,
            short_fill_distance_pct=0.5,
            price_tick_size=0.0001,
            qty_step=0.01,
            min_order_qty=0.01,
            restart=False,
        )
    )


def _short_runtime(*, cycle_index: int = 1) -> RuntimeState:
    first_leg = f"CYCLE_{cycle_index}_SHORT_REDUCE"
    second_leg = f"CYCLE_{cycle_index}_LONG_REDUCE"
    return RuntimeState(
        strategy_state={
            "trade_block_id": "tb-short-pnl",
            "active_cycle_index": cycle_index,
            "cycle_step": STEP_WAITING_FOR_PAIR_FIRST_LEG,
            "next_required_purpose": first_leg,
            "processed_cycle_purposes": [],
            "initial_long_qty": 62.8,
            "initial_short_qty": 125.6,
            "entry_reference_price": 0.7955,
            "pending_cycle_loss_usdt": 0.0,
            "cycle_states": {
                str(cycle_index): {
                    "short_reduce_status": "NONE",
                    "long_reduce_status": "NONE",
                }
            },
            "instrument_rules": {
                "APTUSDT": {"tick_size": 0.0001, "qty_step": 0.01, "min_order_qty": 0.01}
            },
        },
        last_snapshot=HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.7995,
            long_qty=62.8,
            short_qty=125.6,
            long_avg=0.7955,
            short_avg=0.7955,
        ),
    )


def _short_reduce_fill(
    *,
    cycle_index: int,
    closed_pnl: float | None,
) -> FillEvent:
    metadata: dict = {"cycle_index": cycle_index, "cycle_role": "short_reduce"}
    if closed_pnl is not None:
        metadata["closed_pnl"] = closed_pnl
        metadata["confirmed_closed_pnl"] = closed_pnl
    return FillEvent(
        exchange_order_id=f"ex-short-{cycle_index}",
        client_order_id=f"cli-short-{cycle_index}-{id(closed_pnl)}",
        side="short",
        purpose=f"CYCLE_{cycle_index}_SHORT_REDUCE",
        exec_qty=31.4,
        exec_price=0.7995,
        order_type="Market",
        reduce_only=True,
        status="FILLED",
        metadata=metadata,
    )


class ShortFirstLegPnlPersistenceTests(unittest.TestCase):
    def test_runtime_pnl_persisted_and_second_leg_built_cycle_1(self) -> None:
        strategy = _short_strategy()
        runtime = _short_runtime(cycle_index=1)
        fill_event = _short_reduce_fill(cycle_index=1, closed_pnl=-0.1257)

        strategy._advance_cycle_from_fill(fill_event, runtime, _context())

        seq_entry = strategy._get_cycle_sequence_entry(runtime, 1)
        state = runtime.strategy_state
        self.assertAlmostEqual(float(seq_entry["long_add_confirmed_pnl"]), -0.1257)
        self.assertAlmostEqual(float(seq_entry["long_add_loss_usdt"]), 0.1257)
        self.assertAlmostEqual(float(state["pending_cycle_loss_usdt"]), 0.1257)
        self.assertTrue(strategy._get_second_leg_waiting(state))
        self.assertEqual(int(strategy._get_second_leg_pending_cycle(state)), 1)

        captured: list[dict] = []
        ctx = _context()
        ctx.audit.log_event = lambda event, **payload: captured.append({"event": event, **payload})
        with mock.patch.object(strategy, "_maybe_activate_recovery_after_first_leg_fill", return_value=False), mock.patch.object(
            strategy, "_can_submit_cycle_intent", return_value=(True, "ok", {})
        ):
            intents = strategy._build_short_tp_follow_up(runtime.last_snapshot, runtime, ctx)

        purposes = [intent.purpose for intent in intents]
        self.assertIn("CYCLE_1_LONG_REDUCE", purposes)

    def test_runtime_pnl_persisted_for_multiple_cycles(self) -> None:
        for cycle_index in (1, 2, 4):
            with self.subTest(cycle_index=cycle_index):
                strategy = _short_strategy()
                runtime = _short_runtime(cycle_index=cycle_index)
                loss = -0.1 * cycle_index
                fill_event = _short_reduce_fill(cycle_index=cycle_index, closed_pnl=loss)

                strategy._advance_cycle_from_fill(fill_event, runtime, _context())

                seq_entry = strategy._get_cycle_sequence_entry(runtime, cycle_index)
                self.assertAlmostEqual(float(seq_entry["long_add_confirmed_pnl"]), loss)
                self.assertAlmostEqual(float(seq_entry["long_add_loss_usdt"]), abs(loss))

    def test_missing_pnl_does_not_invent_values(self) -> None:
        strategy = _short_strategy()
        runtime = _short_runtime(cycle_index=1)
        fill_event = _short_reduce_fill(cycle_index=1, closed_pnl=None)

        strategy._advance_cycle_from_fill(fill_event, runtime, _context())

        seq_entry = strategy._get_cycle_sequence_entry(runtime, 1)
        self.assertIsNone(seq_entry.get("long_add_confirmed_pnl"))
        self.assertEqual(float(runtime.strategy_state.get("pending_cycle_loss_usdt") or 0.0), 0.0)

        intents = strategy._build_short_tp_follow_up(runtime.last_snapshot, runtime, _context())
        self.assertEqual(intents, [])

    def test_api_refresh_still_used_when_runtime_pnl_missing(self) -> None:
        strategy = _short_strategy()
        runtime = _short_runtime(cycle_index=1)
        fill_event = _short_reduce_fill(cycle_index=1, closed_pnl=None)
        ctx = _context()
        ctx.order_manager.fetch_closed_pnl.return_value = [
            {
                "orderId": fill_event.exchange_order_id,
                "symbol": "APTUSDT",
                "closedPnl": "-0.2",
                "updatedTime": 1,
            }
        ]

        refreshed = strategy._refresh_short_reduce_closed_pnl(
            fill_event=fill_event,
            runtime_state=runtime,
            context=ctx,
            cycle_index=1,
        )
        self.assertTrue(refreshed)
        seq_entry = strategy._get_cycle_sequence_entry(runtime, 1)
        self.assertAlmostEqual(float(seq_entry["long_add_confirmed_pnl"]), -0.2)

    def test_no_double_loss_when_runtime_then_identical_api(self) -> None:
        strategy = _short_strategy()
        runtime = _short_runtime(cycle_index=1)
        fill_event = _short_reduce_fill(cycle_index=1, closed_pnl=-0.1257)
        strategy._advance_cycle_from_fill(fill_event, runtime, _context())
        pending_before = float(runtime.strategy_state["pending_cycle_loss_usdt"])

        ctx = _context()
        ctx.order_manager.fetch_closed_pnl.return_value = [
            {
                "orderId": fill_event.exchange_order_id,
                "symbol": "APTUSDT",
                "closedPnl": "-0.1257",
                "updatedTime": 1,
            }
        ]
        strategy._refresh_short_reduce_closed_pnl(
            fill_event=fill_event,
            runtime_state=runtime,
            context=ctx,
            cycle_index=1,
        )
        self.assertAlmostEqual(
            float(runtime.strategy_state["pending_cycle_loss_usdt"]),
            pending_before,
        )

    def test_invalid_pnl_fail_closed(self) -> None:
        strategy = _short_strategy()
        runtime = _short_runtime(cycle_index=1)
        fill_event = _short_reduce_fill(cycle_index=1, closed_pnl=float("nan"))

        persisted = strategy._maybe_persist_short_first_leg_cycle_loss_from_fill(
            runtime,
            cycle_index=1,
            fill_event=fill_event,
        )
        self.assertFalse(persisted)
        seq_entry = strategy._get_cycle_sequence_entry(runtime, 1)
        self.assertIsNone(seq_entry.get("long_add_confirmed_pnl"))

    def test_long_baseline_unchanged(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(symbol="APTUSDT", target_profit_usdt=0.25, restart=False)
        )
        runtime = RuntimeState(
            strategy_state={
                "trade_block_id": "tb-long",
                "active_cycle_index": 1,
                "cycle_step": STEP_WAITING_FOR_PAIR_FIRST_LEG,
                "processed_cycle_purposes": [],
                "initial_long_qty": 100.0,
                "initial_short_qty": 50.0,
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
        fill_event = FillEvent(
            exchange_order_id="ex-long-add",
            client_order_id="cli-long-add",
            side="long",
            purpose="CYCLE_1_LONG_ADD",
            exec_qty=10.0,
            exec_price=0.59,
            order_type="Market",
            reduce_only=False,
            status="FILLED",
            metadata={
                "cycle_index": 1,
                "cycle_role": "long_add",
                "closed_pnl": -0.05,
                "confirmed_closed_pnl": -0.05,
            },
        )
        strategy._advance_cycle_from_fill(fill_event, runtime, _context())
        seq_entry = strategy._get_cycle_sequence_entry(runtime, 1)
        self.assertAlmostEqual(float(seq_entry["long_add_confirmed_pnl"]), -0.05)


if __name__ == "__main__":
    unittest.main()
