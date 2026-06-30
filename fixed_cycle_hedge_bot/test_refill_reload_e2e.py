#!/usr/bin/env python3
"""Long-primary E2E proofs for refill and recovery-reload lifecycles."""

from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_FIRST_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, ManagedOrder, RuntimeState

TBID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
RELOAD_ID = f"{TBID}:recovery_reload:2"

INITIAL_LONG = 67.2
INITIAL_SHORT = 84.8
OLD_LONG_AVG = 0.744
OLD_SHORT_AVG = 0.7438
NEW_LONG_AVG = 0.752
NEW_SHORT_AVG = 0.751
CURRENT_PRICE = 0.75


def _jtousdt_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.1"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.1"),
        "tick_size": Decimal("0.0001"),
    }


def _long_bot_strategy(*, base_notional: float = 50.0) -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="long_bot_1",
            strategy_side="long",
            symbol="JTOUSDT",
            restart=False,
            qty_step=0.1,
            min_order_qty=0.1,
            min_notional_usdt=5.0,
            price_tick_size=0.0001,
            base_notional_usdt=base_notional,
            hedge_ratio_short=1.25,
        )
    )


def _context(*, order_manager: mock.Mock | None = None) -> StrategyContext:
    cancel_mock = mock.Mock()
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_refill_reload_e2e")),
        runtime_name="test_runtime",
        symbol="JTOUSDT",
        category="linear",
        min_order_value=5.0,
        order_manager=order_manager or mock.Mock(cancel_order=mock.Mock(return_value=True)),
        cancel_open_orders_by_purpose=cancel_mock,
    )


def _managed_order(
    *,
    client_id: str,
    purpose: str,
    side: str,
    qty: float,
    price: float,
    exchange_id: str,
) -> ManagedOrder:
    return ManagedOrder(
        client_order_id=client_id,
        side=side,
        qty=qty,
        purpose=purpose,
        price=price,
        order_type="Limit",
        reduce_only=True,
        exchange_order_id=exchange_id,
        status="OPEN",
        remaining_qty=qty,
        metadata={"trigger_price": price},
    )


def _active_refill_trade_state(*, cycle_completed: int = 4) -> dict:
    return {
        "trade_block_id": TBID,
        "initial_entry_confirmed": True,
        "initial_structure_built": True,
        "initial_long_qty": INITIAL_LONG,
        "initial_short_qty": INITIAL_SHORT,
        "entry_reference_price": OLD_LONG_AVG,
        "cycle_completed_count": cycle_completed,
        "cycle_pair_count": cycle_completed,
        "active_cycle_index": cycle_completed,
        "cycle_step": STEP_WAITING_FOR_PAIR_FIRST_LEG,
        "next_required_purpose": f"CYCLE_{cycle_completed + 1}_LONG_ADD",
        "exit_rebuild_allowed": True,
        "pending_cycle_loss_usdt": 0.0,
    }


def _snapshot(
    *,
    long_qty: float,
    short_qty: float,
    long_avg: float,
    short_avg: float,
) -> HedgeSnapshot:
    return HedgeSnapshot(
        symbol="JTOUSDT",
        current_price=CURRENT_PRICE,
        long_qty=long_qty,
        short_qty=short_qty,
        long_avg=long_avg,
        short_avg=short_avg,
    )


class RefillE2ELifecycleTests(unittest.TestCase):
    def test_refill_e2e_cancels_old_orders_reconciles_and_rebuilds_from_new_avg(self) -> None:
        strategy = _long_bot_strategy()
        state = _active_refill_trade_state()
        runtime_state = RuntimeState(strategy_state=state)
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()

        old_long_tp_price = 0.8012
        old_short_sl_price = 0.7998
        runtime_state.active_orders = {
            "exit-long": _managed_order(
                client_id="exit-long",
                purpose="LONG_TP_EXIT",
                side="long",
                qty=INITIAL_LONG,
                price=old_long_tp_price,
                exchange_id="ex-long-tp",
            ),
            "exit-short": _managed_order(
                client_id="exit-short",
                purpose="SHORT_SL_EXIT",
                side="short",
                qty=INITIAL_SHORT,
                price=old_short_sl_price,
                exchange_id="ex-short-sl",
            ),
            "cycle-add": _managed_order(
                client_id="cycle-add",
                purpose="CYCLE_5_LONG_ADD",
                side="long",
                qty=21.2,
                price=0.735,
                exchange_id="ex-cycle-add",
            ),
        }
        runtime_state.exchange_to_client_id = {
            "ex-long-tp": "exit-long",
            "ex-short-sl": "exit-short",
            "ex-cycle-add": "cycle-add",
        }

        long_before = 50.0
        short_before = 60.0
        pre_refill_snapshot = _snapshot(
            long_qty=long_before,
            short_qty=short_before,
            long_avg=OLD_LONG_AVG,
            short_avg=OLD_SHORT_AVG,
        )
        runtime_state.last_snapshot = pre_refill_snapshot

        strategy._enter_refill_mode(state, reason="cycle_pair_completed", cycle_index=4, symbol="JTOUSDT")
        self.assertTrue(state.get("refill_exit_orders_cancel_required"))

        order_manager = mock.Mock(cancel_order=mock.Mock(return_value=True))
        context = _context(order_manager=order_manager)

        cancel_ok = strategy._cancel_refill_exit_orders(runtime_state, context, state)
        self.assertTrue(cancel_ok)
        self.assertFalse(state.get("refill_exit_orders_cancel_required"))
        self.assertEqual(order_manager.cancel_order.call_count, 2)
        remaining_purposes = {order.purpose for order in runtime_state.active_orders.values()}
        self.assertNotIn("LONG_TP_EXIT", remaining_purposes)
        self.assertNotIn("SHORT_SL_EXIT", remaining_purposes)
        self.assertIn("CYCLE_5_LONG_ADD", remaining_purposes)

        refill_intents = strategy._build_entry_intents(pre_refill_snapshot, runtime_state, context)
        purposes = {intent.purpose for intent in refill_intents}
        self.assertEqual(purposes, {"REFILL_LONG", "REFILL_SHORT"})
        refill_long_qty = sum(intent.qty for intent in refill_intents if intent.purpose == "REFILL_LONG")
        refill_short_qty = sum(intent.qty for intent in refill_intents if intent.side == "short")
        self.assertAlmostEqual(refill_long_qty, INITIAL_LONG - long_before, places=1)
        self.assertAlmostEqual(refill_short_qty, 24.7, places=1)
        self.assertGreater(refill_short_qty, 0.0)

        state["refill_long_filled"] = True
        state["refill_short_filled"] = True
        state["refill_fills_complete"] = True
        state["refill_completion_pending_reconcile"] = True

        post_refill_snapshot = _snapshot(
            long_qty=INITIAL_LONG,
            short_qty=INITIAL_SHORT,
            long_avg=NEW_LONG_AVG,
            short_avg=NEW_SHORT_AVG,
        )
        runtime_state.last_snapshot = post_refill_snapshot

        completed = strategy._maybe_complete_refill_after_reconcile(
            post_refill_snapshot,
            runtime_state,
            context,
            reason="test_refill_reconcile",
        )
        self.assertTrue(completed)
        self.assertFalse(state.get("refill_pending"))
        self.assertTrue(state.get("force_exit_rebuild"))
        self.assertFalse(state.get("cycle_waiting_for_short_tp"))

        old_break_even, _ = strategy._calculate_break_even(pre_refill_snapshot, runtime_state)
        old_tp = strategy._calculate_tp_price(old_break_even, pre_refill_snapshot, runtime_state)
        new_break_even, _ = strategy._calculate_break_even(post_refill_snapshot, runtime_state)
        new_tp = strategy._calculate_tp_price(new_break_even, post_refill_snapshot, runtime_state)
        self.assertNotAlmostEqual(old_tp, new_tp, places=6)

        exit_intents = strategy._build_exit_intents(
            post_refill_snapshot,
            runtime_state,
            int(state.get("active_cycle_index") or 0),
            new_break_even,
            new_tp,
            hard_stop_active=False,
            context=context,
            force_exit_rebuild=True,
        )
        self.assertGreater(len(exit_intents), 0)
        exit_purposes = {intent.purpose for intent in exit_intents}
        self.assertIn("LONG_TP_EXIT", exit_purposes)
        self.assertIn("SHORT_SL_EXIT", exit_purposes)

        long_tp_intents = [intent for intent in exit_intents if intent.purpose == "LONG_TP_EXIT"]
        short_sl_intents = [intent for intent in exit_intents if intent.purpose == "SHORT_SL_EXIT"]
        self.assertTrue(long_tp_intents)
        self.assertTrue(short_sl_intents)
        rebuilt_long_tp = float(long_tp_intents[0].trigger_price or long_tp_intents[0].price or 0.0)
        rebuilt_short_sl = float(short_sl_intents[0].trigger_price or short_sl_intents[0].price or 0.0)
        self.assertNotAlmostEqual(rebuilt_long_tp, old_long_tp_price, places=4)
        self.assertNotAlmostEqual(rebuilt_short_sl, old_short_sl_price, places=4)
        self.assertAlmostEqual(rebuilt_long_tp, new_tp, places=4)

        stale_exit_orders = [
            order
            for order in runtime_state.active_orders.values()
            if order.purpose in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
            and float(getattr(order, "metadata", {}).get("trigger_price", order.price or 0.0))
            in {old_long_tp_price, old_short_sl_price}
        ]
        self.assertEqual(stale_exit_orders, [])

        self.assertAlmostEqual(post_refill_snapshot.long_qty, INITIAL_LONG)
        self.assertAlmostEqual(post_refill_snapshot.short_qty, INITIAL_SHORT)
        self.assertAlmostEqual(post_refill_snapshot.long_avg, NEW_LONG_AVG)
        self.assertAlmostEqual(post_refill_snapshot.short_avg, NEW_SHORT_AVG)


class RecoveryReloadE2ELifecycleTests(unittest.TestCase):
    def test_recovery_reload_cancels_reconciles_and_rebuilds_from_reconciled_avg(self) -> None:
        strategy = _long_bot_strategy(base_notional=50.0)
        state = _active_refill_trade_state(cycle_completed=2)
        state.update(
            {
                "recovery_required": True,
                "recovery_in_progress": True,
                "recovery_reference_cycle_index": 2,
                "recovery_reload_id": RELOAD_ID,
                "recovery_activation_reason": "time_distance_refill",
                "cycle_waiting_for_short_tp": True,
                "short_tp_pending_cycle": 2,
                "pending_short_cycle_index": 2,
            }
        )
        runtime_state = RuntimeState(strategy_state=state)
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()

        old_long_tp_price = 0.8012
        old_cycle_trigger = 0.7044
        runtime_state.active_orders = {
            "exit-long": _managed_order(
                client_id="exit-long",
                purpose="LONG_TP_EXIT",
                side="long",
                qty=INITIAL_LONG,
                price=old_long_tp_price,
                exchange_id="ex-long-tp",
            ),
            "cycle-short": _managed_order(
                client_id="cycle-short",
                purpose="CYCLE_2_SHORT_REDUCE",
                side="short",
                qty=21.2,
                price=old_cycle_trigger,
                exchange_id="ex-cycle-short",
            ),
        }
        runtime_state.exchange_to_client_id = {
            "ex-long-tp": "exit-long",
            "ex-cycle-short": "cycle-short",
        }

        pre_reload_snapshot = _snapshot(
            long_qty=INITIAL_LONG,
            short_qty=INITIAL_SHORT,
            long_avg=OLD_LONG_AVG,
            short_avg=OLD_SHORT_AVG,
        )
        runtime_state.last_snapshot = pre_reload_snapshot

        order_manager = mock.Mock(cancel_order=mock.Mock(return_value=True))
        context = _context(order_manager=order_manager)

        with mock.patch.object(strategy, "_ensure_recovery_wallet_transfer", return_value=True):
            reload_intents = strategy._build_recovery_refill_intents(
                pre_reload_snapshot,
                runtime_state,
                context,
            )

        self.assertIsNotNone(reload_intents)
        assert reload_intents is not None
        reload_purposes = {intent.purpose for intent in reload_intents}
        self.assertEqual(
            reload_purposes,
            {"RECOVERY_RELOAD_LONG_ENTRY", "RECOVERY_RELOAD_SHORT_ENTRY"},
        )
        self.assertGreaterEqual(order_manager.cancel_order.call_count, 1)
        remaining_purposes = {order.purpose for order in runtime_state.active_orders.values()}
        self.assertNotIn("LONG_TP_EXIT", remaining_purposes)
        self.assertNotIn("CYCLE_2_SHORT_REDUCE", remaining_purposes)
        self.assertFalse(state.get("recovery_reload_rest_reconcile_confirmed"))

        reconciled_snapshot = _snapshot(
            long_qty=INITIAL_LONG,
            short_qty=INITIAL_SHORT,
            long_avg=NEW_LONG_AVG,
            short_avg=NEW_SHORT_AVG,
        )
        reconciled_snapshot.source = "recovery_reload_rest"

        def _refresh_snapshot(source: str) -> HedgeSnapshot:
            self.assertEqual(source, "recovery_reload_rest")
            return reconciled_snapshot

        context.refresh_snapshot = _refresh_snapshot  # type: ignore[method-assign]

        for side, purpose in (
            ("long", "RECOVERY_RELOAD_LONG_ENTRY"),
            ("short", "RECOVERY_RELOAD_SHORT_ENTRY"),
        ):
            fill_qty = next(intent.qty for intent in reload_intents if intent.purpose == purpose)
            strategy._handle_recovery_refill_fill(
                FillEvent(
                    exchange_order_id=f"reload-{side}",
                    client_order_id=f"client-reload-{side}",
                    side=side,
                    purpose=purpose,
                    exec_qty=fill_qty,
                    exec_price=NEW_LONG_AVG if side == "long" else NEW_SHORT_AVG,
                    order_type="Market",
                    reduce_only=False,
                    status="FILLED",
                ),
                pre_reload_snapshot,
                runtime_state,
                context,
            )

        self.assertTrue(state.get("recovery_reload_rest_reconcile_confirmed"))
        self.assertFalse(state.get("recovery_reload_rest_reconcile_required"))
        self.assertTrue(state.get("post_refill_structure_rebuild_required"))
        self.assertFalse(state.get("refill_pending"))

        runtime_state.last_snapshot = reconciled_snapshot
        state["cycle_states"] = {
            "2": {
                "long_add_status": "PROCESSED",
                "short_tp_status": "NONE",
                "long_add_confirmed_pnl": -0.25,
                "complete": False,
            }
        }
        state["cycle_state"] = {
            "symbol": "JTOUSDT",
            "long_fills": {
                "2": {
                    "price": OLD_LONG_AVG,
                    "incremental_qty": 21.2,
                    "closed_pnl": -0.25,
                    "confirmed_closed_pnl": -0.25,
                }
            },
            "short_fills": {},
            "long_cycle_index": 2,
            "short_cycle_index": 0,
        }
        state["processed_cycle_purposes"] = ["CYCLE_2_LONG_ADD"]
        state["pending_cycle_loss_usdt"] = 0.25

        with mock.patch.object(
            strategy,
            "_maybe_activate_recovery_after_first_leg_fill",
            return_value=False,
        ), mock.patch.object(
            strategy,
            "_can_submit_cycle_intent",
            return_value=(True, "ok", {}),
        ), mock.patch.object(
            strategy,
            "_fixed_short_cycle_qty",
            return_value=21.2,
        ):
            cycle_intents = strategy._build_short_tp_follow_up(
                reconciled_snapshot,
                runtime_state,
                context,
            )

        self.assertGreater(len(cycle_intents), 0)
        rebuilt_trigger = float(cycle_intents[0].trigger_price or 0.0)
        self.assertNotAlmostEqual(rebuilt_trigger, old_cycle_trigger, places=3)
        self.assertGreater(rebuilt_trigger, 0.0)

        stale_orders = [
            order
            for order in runtime_state.active_orders.values()
            if order.purpose in {"LONG_TP_EXIT", "CYCLE_2_SHORT_REDUCE"}
        ]
        self.assertEqual(stale_orders, [])
        self.assertAlmostEqual(reconciled_snapshot.long_avg, NEW_LONG_AVG)
        self.assertAlmostEqual(reconciled_snapshot.short_avg, NEW_SHORT_AVG)


if __name__ == "__main__":
    unittest.main()
