#!/usr/bin/env python3
"""Short-primary E2E proofs for refill and recovery-reload lifecycles."""

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
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import FillEvent, HedgeSnapshot, ManagedOrder, RuntimeState

TBID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
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


def _short_bot_strategy(*, base_notional: float = 50.0) -> ShortFixedCycleHedgeStrategy:
    return ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="short_bot_1",
            strategy_side="short",
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
        audit=AuditLogger(logging.getLogger("test_short_primary_refill_reload_e2e")),
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
        "entry_reference_price": OLD_SHORT_AVG,
        "cycle_completed_count": cycle_completed,
        "cycle_pair_count": cycle_completed,
        "active_cycle_index": cycle_completed,
        "cycle_step": STEP_WAITING_FOR_PAIR_FIRST_LEG,
        "next_required_purpose": f"CYCLE_{cycle_completed + 1}_SHORT_REDUCE",
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


class ShortPrimaryRefillE2ELifecycleTests(unittest.TestCase):
    def test_short_primary_refill_e2e_cancels_old_exits_reconciles_and_rebuilds(self) -> None:
        strategy = _short_bot_strategy()
        state = _active_refill_trade_state()
        runtime_state = RuntimeState(strategy_state=state)
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()

        old_short_tp_price = 0.8012
        old_long_sl_price = 0.7998
        runtime_state.active_orders = {
            "exit-short": _managed_order(
                client_id="exit-short",
                purpose="SHORT_TP_EXIT",
                side="short",
                qty=INITIAL_SHORT,
                price=old_short_tp_price,
                exchange_id="ex-short-tp",
            ),
            "exit-long": _managed_order(
                client_id="exit-long",
                purpose="LONG_SL_EXIT",
                side="long",
                qty=INITIAL_LONG,
                price=old_long_sl_price,
                exchange_id="ex-long-sl",
            ),
            "cycle-short": _managed_order(
                client_id="cycle-short",
                purpose="CYCLE_5_SHORT_REDUCE",
                side="short",
                qty=21.2,
                price=0.735,
                exchange_id="ex-cycle-short",
            ),
        }
        runtime_state.exchange_to_client_id = {
            "ex-short-tp": "exit-short",
            "ex-long-sl": "exit-long",
            "ex-cycle-short": "cycle-short",
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
        self.assertEqual(order_manager.cancel_order.call_count, 3)
        cancelled_exchange_ids = {
            call.args[0] for call in order_manager.cancel_order.call_args_list
        }
        self.assertEqual(
            cancelled_exchange_ids,
            {"ex-short-tp", "ex-long-sl", "ex-cycle-short"},
        )
        self.assertNotIn("cycle-short", runtime_state.active_orders)
        self.assertNotIn("ex-cycle-short", runtime_state.exchange_to_client_id)
        remaining_purposes = {order.purpose for order in runtime_state.active_orders.values()}
        self.assertNotIn("SHORT_TP_EXIT", remaining_purposes)
        self.assertNotIn("LONG_SL_EXIT", remaining_purposes)
        self.assertNotIn("CYCLE_5_SHORT_REDUCE", remaining_purposes)

        refill_intents = strategy._build_entry_intents(pre_refill_snapshot, runtime_state, context)
        purposes = {intent.purpose for intent in refill_intents}
        self.assertEqual(purposes, {"REFILL_LONG", "REFILL_SHORT"})

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
        self.assertFalse(state.get("cycle_waiting_for_long_reduce"))

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
        self.assertIn("SHORT_TP_EXIT", exit_purposes)
        self.assertIn("LONG_SL_EXIT", exit_purposes)

        short_tp_intents = [intent for intent in exit_intents if intent.purpose == "SHORT_TP_EXIT"]
        long_sl_intents = [intent for intent in exit_intents if intent.purpose == "LONG_SL_EXIT"]
        rebuilt_short_tp = float(short_tp_intents[0].trigger_price or short_tp_intents[0].price or 0.0)
        rebuilt_long_sl = float(long_sl_intents[0].trigger_price or long_sl_intents[0].price or 0.0)
        self.assertNotAlmostEqual(rebuilt_short_tp, old_short_tp_price, places=4)
        self.assertNotAlmostEqual(rebuilt_long_sl, old_long_sl_price, places=4)


class ShortPrimaryRecoveryReloadE2ELifecycleTests(unittest.TestCase):
    def test_short_primary_recovery_reload_cancels_cycle_orders_and_rebuilds_from_reconciled_avg(
        self,
    ) -> None:
        strategy = _short_bot_strategy(base_notional=50.0)
        state = _active_refill_trade_state(cycle_completed=2)
        state.update(
            {
                "recovery_required": True,
                "recovery_in_progress": True,
                "recovery_reference_cycle_index": 2,
                "recovery_reload_id": RELOAD_ID,
                "recovery_activation_reason": "time_distance_refill",
                "cycle_waiting_for_long_reduce": True,
                "long_reduce_pending_cycle": 2,
                "pending_long_cycle_index": 2,
            }
        )
        runtime_state = RuntimeState(strategy_state=state)
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()

        old_short_tp_price = 0.8012
        old_cycle_trigger = 0.752
        runtime_state.active_orders = {
            "exit-short": _managed_order(
                client_id="exit-short",
                purpose="SHORT_TP_EXIT",
                side="short",
                qty=INITIAL_SHORT,
                price=old_short_tp_price,
                exchange_id="ex-short-tp",
            ),
            "cycle-long": _managed_order(
                client_id="cycle-long",
                purpose="CYCLE_2_LONG_REDUCE",
                side="long",
                qty=21.2,
                price=old_cycle_trigger,
                exchange_id="ex-cycle-long",
            ),
        }
        runtime_state.exchange_to_client_id = {
            "ex-short-tp": "exit-short",
            "ex-cycle-long": "cycle-long",
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
        self.assertNotIn("SHORT_TP_EXIT", remaining_purposes)
        self.assertNotIn("CYCLE_2_LONG_REDUCE", remaining_purposes)
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

        runtime_state.last_snapshot = reconciled_snapshot
        state["cycle_states"] = {
            "2": {
                "short_reduce_status": "PROCESSED",
                "long_reduce_status": "NONE",
                "long_add_confirmed_pnl": -0.25,
                "short_reduce_fill_price": OLD_SHORT_AVG,
                "complete": False,
            }
        }
        state["cycle_state"] = {
            "symbol": "JTOUSDT",
            "short_fills": {
                "2": {
                    "price": OLD_SHORT_AVG,
                    "incremental_qty": 21.2,
                    "closed_pnl": -0.25,
                    "confirmed_closed_pnl": -0.25,
                }
            },
            "long_fills": {},
            "short_cycle_index": 2,
            "long_cycle_index": 0,
        }
        state["processed_cycle_purposes"] = ["CYCLE_2_SHORT_REDUCE"]
        state["pending_cycle_loss_usdt"] = 0.25
        state["cycle_waiting_for_long_reduce"] = True
        state["long_reduce_pending_cycle"] = 2

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
            "_fixed_long_cycle_qty",
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
        self.assertEqual(cycle_intents[0].purpose, "CYCLE_2_LONG_REDUCE")

        stale_orders = [
            order
            for order in runtime_state.active_orders.values()
            if order.purpose in {"SHORT_TP_EXIT", "CYCLE_2_LONG_REDUCE"}
        ]
        self.assertEqual(stale_orders, [])
        self.assertAlmostEqual(reconciled_snapshot.long_avg, NEW_LONG_AVG)
        self.assertAlmostEqual(reconciled_snapshot.short_avg, NEW_SHORT_AVG)


if __name__ == "__main__":
    unittest.main()
