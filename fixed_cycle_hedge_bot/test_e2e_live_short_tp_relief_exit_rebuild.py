#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState, StrategyIntent


def _context() -> StrategyContext:
    """StrategyContext mit Fake-Order-Manager und Cancel-Callback wie in anderen Live-Tests."""
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_e2e_live_short_tp_relief_exit_rebuild")),
        runtime_name="test_runtime",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        order_manager=mock.Mock(),
        cancel_open_orders_by_purpose=mock.Mock(),
    )


def _relief_strategy() -> FixedCycleHedgeStrategy:
    """Long-Primary-Strategie mit aktiviertem Live-Short-TP-Relief."""
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="long_bot_1",
            strategy_side="long",
            symbol="APTUSDT",
            price_tick_size=0.0001,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.125,
            order_fee_rate_pct=0.055,
            cycle_short_tp_relief_enabled=True,
            cycle_short_tp_relief_start_cycle_index=4,
            cycle_short_tp_relief_max_distance_pct_from_long_fill=4.0,
            cycle_short_tp_relief_carry_uncovered_loss_to_exit=True,
        )
    )


def _instrument_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.01"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.01"),
        "tick_size": Decimal("0.0001"),
    }


def _snapshot_after_long_add() -> HedgeSnapshot:
    """Angelehnt an _snapshot_after_cycle_long_add in test_cycle_fill_exit_rebuild."""
    return HedgeSnapshot(
        symbol="APTUSDT",
        current_price=0.905,
        long_qty=109.721,
        short_qty=54.860,
        long_avg=0.9114,
        short_avg=0.9114,
    )


def _cycle_state_with_pending_short_tp(*, cycle_index: int) -> dict[str, object]:
    """Basis-Cycle-State mit wartendem Short-TP für einen gegebenen Cycle."""
    return {
        "initial_entry_confirmed": True,
        "initial_structure_built": True,
        "exit_rebuild_allowed": True,
        "exit_orders_stale_after_structure_fill": True,
        "pending_loss_updated_in_fill": True,
        "pending_cycle_loss_usdt": 0.0,
        "cycle_completed_count": cycle_index - 1,
        "cycle_pair_count": max(cycle_index - 1, 1),
        "current_effective_cycle": cycle_index,
        "current_long_cycle_index": cycle_index,
        "pending_short_cycle_index": cycle_index,
        "cycle_waiting_for_short_tp": True,
        "short_tp_pending_cycle": cycle_index,
        "last_refill_completed_cycle_index": 0,
        "trade_block_id": "tb-e2e-1",
    }


class LiveShortTpReliefE2EExitRebuildTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = _relief_strategy()

    def test_normal_relief_flow_and_exit_rebuild(self) -> None:
        """Normaler Relief-Ablauf: Cap, Carry-Loss, Exit-Rebuild mit korrekten Exits."""
        runtime_state = RuntimeState(strategy_state=_cycle_state_with_pending_short_tp(cycle_index=4))
        runtime_state.instrument_rules["APTUSDT"] = _instrument_rules()
        snapshot = _snapshot_after_long_add()
        context = _context()

        # Short-TP-Followup über den echten Live-Pfad bauen und Relief anwenden.
        short_intents = self.strategy._build_short_tp_follow_up(snapshot, runtime_state, context)
        self.assertTrue(short_intents, "expected short TP follow-up intents")
        original_trigger_prices = [float(intent.trigger_price or 0.0) for intent in short_intents]

        relieved_intents = self.strategy._apply_live_short_tp_relief(
            snapshot,
            runtime_state,
            short_intents,
        )
        self.assertEqual(len(relieved_intents), len(short_intents))

        # Mindestens ein Intent muss einen geänderten Trigger-Preis haben.
        relieved_prices = [float(intent.trigger_price or 0.0) for intent in relieved_intents]
        self.assertNotEqual(original_trigger_prices, relieved_prices)

        # Relief-State muss Carry-Loss für den Trade-Block enthalten.
        state = runtime_state.strategy_state
        relief_state = state.get("short_tp_relief_state") or {}
        carry_by_block = relief_state.get("carry_loss_by_trade_block") or {}
        self.assertIn("tb-e2e-1", carry_by_block)
        uncovered_loss_total = float(carry_by_block["tb-e2e-1"] or 0.0)
        self.assertGreater(uncovered_loss_total, 0.0)

        # Effektiver Pending-Loss in der Exit-Math muss Carry berücksichtigen.
        base_pending = float(state.get("pending_cycle_loss_usdt") or 0.0)
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        projection = self.strategy._calculate_tp_projection(break_even, snapshot, runtime_state)
        self.assertAlmostEqual(
            projection.pending_cycle_loss_usdt,
            base_pending + uncovered_loss_total,
            places=8,
        )

        # Exit-Rebuild aus diesem Zustand mit den Relief-Intent(s) als Pending-Cycle-Intents.
        tp_price = self.strategy._calculate_tp_price(break_even, snapshot, runtime_state)
        exit_intents = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=4,
            break_even_price=break_even,
            tp_price=tp_price,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
            pending_cycle_intents=relieved_intents,
        )
        self.assertTrue(exit_intents, "expected rebuilt exit intents after relief")

        purposes = {intent.purpose for intent in exit_intents}
        self.assertIn("LONG_TP_EXIT", purposes)
        self.assertIn("SHORT_SL_EXIT", purposes)

        # Exits müssen Reduce-Only sein und konsistente Seite/Qty-Zuordnung haben.
        for intent in exit_intents:
            self.assertTrue(
                getattr(intent, "reduce_only", True),
                f"exit intent {intent.purpose} should be reduce_only",
            )
            if intent.purpose == "LONG_TP_EXIT":
                self.assertEqual(intent.side, "long")
                self.assertGreater(intent.qty or 0.0, 0.0)
            if intent.purpose == "SHORT_SL_EXIT":
                self.assertEqual(intent.side, "short")
                self.assertGreater(intent.qty or 0.0, 0.0)

    def test_double_processing_does_not_double_count_carry_or_exits(self) -> None:
        """Doppelte Verarbeitung desselben Followups darf Carry und Exit-Rebuild nicht verdoppeln."""
        runtime_state = RuntimeState(strategy_state=_cycle_state_with_pending_short_tp(cycle_index=4))
        runtime_state.instrument_rules["APTUSDT"] = _instrument_rules()
        snapshot = _snapshot_after_long_add()
        context = _context()

        short_intents = self.strategy._build_short_tp_follow_up(snapshot, runtime_state, context)
        self.assertTrue(short_intents, "expected short TP follow-up intents")

        # Erster Relief-Lauf.
        relieved_once = self.strategy._apply_live_short_tp_relief(
            snapshot,
            runtime_state,
            list(short_intents),
        )
        state = runtime_state.strategy_state
        relief_state = state.get("short_tp_relief_state") or {}
        carry_by_block = relief_state.get("carry_loss_by_trade_block") or {}
        first_carry = float(carry_by_block.get("tb-e2e-1") or 0.0)
        self.assertGreater(first_carry, 0.0)

        # Zweiter Relief-Lauf mit den bereits geänderten Intents.
        relieved_twice = self.strategy._apply_live_short_tp_relief(
            snapshot,
            runtime_state,
            list(relieved_once),
        )
        relief_state_second = runtime_state.strategy_state.get("short_tp_relief_state") or {}
        carry_by_block_second = relief_state_second.get("carry_loss_by_trade_block") or {}
        second_carry = float(carry_by_block_second.get("tb-e2e-1") or 0.0)
        self.assertAlmostEqual(second_carry, first_carry, places=8)

        # Exit-Rebuild sollte aus beiden Pfaden dasselbe Exit-Set liefern.
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime_state)
        tp_price = self.strategy._calculate_tp_price(break_even, snapshot, runtime_state)

        exits_once = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=4,
            break_even_price=break_even,
            tp_price=tp_price,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
            pending_cycle_intents=relieved_once,
        )
        exits_twice = self.strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=4,
            break_even_price=break_even,
            tp_price=tp_price,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
            pending_cycle_intents=relieved_twice,
        )

        purposes_once = sorted((intent.purpose, float(intent.qty or 0.0), float(intent.trigger_price or 0.0)) for intent in exits_once)
        purposes_twice = sorted((intent.purpose, float(intent.qty or 0.0), float(intent.trigger_price or 0.0)) for intent in exits_twice)
        self.assertEqual(purposes_once, purposes_twice)

    def test_partial_fill_and_restart_paths_are_not_yet_covered(self) -> None:
        """Platzhalter: Teilfills/Restart-E2E erfordern präzisere Spezifikation und bleiben vorerst offen."""
        self.skipTest("E2E-Tests für Teilfills/Restart/Cancel-Rebuild-Pfade sind noch nicht spezifiziert.")


if __name__ == "__main__":
    unittest.main()

