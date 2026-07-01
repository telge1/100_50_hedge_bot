#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState, StrategyIntent


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_cycle_fill_exit_rebuild")),
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


def _aptusdt_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.01"),
        "tick_size": Decimal("0.0001"),
    }


def _snapshot_after_cycle_long_add() -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="APTUSDT",
        current_price=0.905,
        long_qty=109.721,
        short_qty=54.860,
        long_avg=0.9114,
        short_avg=0.9114,
    )


def _cycle_state_after_long_add(*, cycle_index: int) -> dict[str, object]:
    return {
        "initial_entry_confirmed": True,
        "initial_structure_built": True,
        "exit_rebuild_allowed": True,
        "force_exit_rebuild": True,
        "pending_loss_updated_in_fill": True,
        "exit_orders_stale_after_structure_fill": True,
        "pending_cycle_loss_usdt": 0.0,
        "cycle_completed_count": cycle_index - 1,
        "cycle_pair_count": max(cycle_index - 1, 1),
        "current_effective_cycle": cycle_index,
        "current_long_cycle_index": cycle_index,
        "pending_short_cycle_index": cycle_index,
        "cycle_waiting_for_short_tp": True,
        "short_tp_pending_cycle": cycle_index,
        "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
        "cycle_long_add_filled": True,
        "last_refill_completed_cycle_index": 0,
    }


class CycleFillExitRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = _strategy()

    def _build_exits_after_long_add(
        self,
        *,
        cycle_index: int,
        pending_short_intent: StrategyIntent | None,
    ) -> list[StrategyIntent]:
        runtime_state = RuntimeState(strategy_state=_cycle_state_after_long_add(cycle_index=cycle_index))
        runtime_state.instrument_rules["APTUSDT"] = _aptusdt_rules()
        snapshot = _snapshot_after_cycle_long_add()
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        tp_price = self.strategy._calculate_tp_price(break_even, snapshot, runtime_state)
        pending = [pending_short_intent] if pending_short_intent is not None else None
        return self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=cycle_index,
            break_even_price=break_even,
            tp_price=tp_price,
            hard_stop_active=False,
            context=_context(),
            force_exit_rebuild=True,
            pending_cycle_intents=pending,
        )

    def test_cycle_1_long_add_rebuilds_both_exit_orders(self) -> None:
        pending_short = StrategyIntent(
            side="short",
            qty=4.7,
            purpose="CYCLE_1_SHORT_REDUCE",
            order_type="Market",
            trigger_price=1.94,
            metadata={"cycle_index": 1},
        )
        intents = self._build_exits_after_long_add(cycle_index=1, pending_short_intent=pending_short)
        purposes = {intent.purpose for intent in intents}
        self.assertIn("LONG_TP_EXIT", purposes)
        self.assertIn("SHORT_SL_EXIT", purposes)

    def test_cycle_2_long_add_rebuilds_both_exit_orders_without_active_short_order(self) -> None:
        pending_short = StrategyIntent(
            side="short",
            qty=4.738,
            purpose="CYCLE_2_SHORT_REDUCE",
            order_type="Market",
            trigger_price=1.9092,
            metadata={"cycle_index": 2},
        )
        intents = self._build_exits_after_long_add(cycle_index=2, pending_short_intent=pending_short)
        purposes = {intent.purpose for intent in intents}
        self.assertIn("LONG_TP_EXIT", purposes)
        self.assertIn("SHORT_SL_EXIT", purposes)

    def test_rebuild_structure_submits_exits_before_cycle_follow_up(self) -> None:
        runtime_state = RuntimeState(strategy_state=_cycle_state_after_long_add(cycle_index=2))
        runtime_state.instrument_rules["APTUSDT"] = _aptusdt_rules()
        runtime_state.strategy_state["force_exit_rebuild"] = True
        runtime_state.strategy_state["pending_loss_updated_in_fill"] = True
        snapshot = _snapshot_after_cycle_long_add()
        context = _context()
        exit_intents = [
            StrategyIntent(
                side="long",
                qty=109.721,
                purpose="LONG_TP_EXIT",
                order_type="Market",
                trigger_price=0.9213,
            ),
            StrategyIntent(
                side="short",
                qty=54.860,
                purpose="SHORT_SL_EXIT",
                order_type="Market",
                trigger_price=0.9213,
            ),
        ]
        downside_intents = [
            StrategyIntent(
                side="short",
                qty=4.738,
                purpose="CYCLE_2_SHORT_REDUCE",
                order_type="Market",
                trigger_price=1.9092,
            )
        ]
        with mock.patch.object(
            self.strategy,
            "_build_downside_cycle_intents",
            return_value=downside_intents,
        ) as downside_mock, mock.patch.object(
            self.strategy,
            "_build_exit_intents",
            return_value=exit_intents,
        ) as exit_mock, mock.patch.object(
            self.strategy,
            "_sync_state_from_snapshot",
        ), mock.patch.object(
            self.strategy,
            "_update_initial_entry_confirmation",
        ), mock.patch.object(
            self.strategy,
            "_cycle_build_block_active",
            return_value=False,
        ):
            intents = self.strategy._rebuild_structure(
                snapshot,
                runtime_state,
                context,
                reason="fill_reconcile",
            )
        downside_mock.assert_called_once()
        exit_mock.assert_called_once()
        kwargs = exit_mock.call_args.kwargs
        self.assertEqual(kwargs.get("pending_cycle_intents"), downside_intents)
        self.assertTrue(intents)
        first_exit_index = next(
            index
            for index, intent in enumerate(intents)
            if intent.purpose in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        )
        first_cycle_index = next(
            index
            for index, intent in enumerate(intents)
            if "CYCLE_2_SHORT_REDUCE" in str(intent.purpose)
        )
        self.assertLess(first_exit_index, first_cycle_index)


if __name__ == "__main__":
    unittest.main()
