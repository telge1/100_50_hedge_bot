#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.cycle_submit_identity import cycle_submit_identity
from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeConfig, FixedCycleHedgeStrategy
from fixed_cycle_hedge_bot.models import HedgeSnapshot, ManagedOrder, RuntimeState, StrategyIntent
from fixed_cycle_hedge_bot.runtime import GenericHedgeRuntime, GenericRuntimeConfig


def _split_metadata(*, cycle_index: int, stage_index: int, stage_count: int = 2) -> dict:
    return {
        "cycle_index": cycle_index,
        "cycle_role": "short_reduce",
        "normal_cycle_second_leg_split": True,
        "split_cycle_index": cycle_index,
        "split_stage_index": stage_index,
        "split_stage_count": stage_count,
        "split_total_qty": 17.83,
        "trigger_price": 0.589,
        "trigger_direction": 2,
        "trigger_by": "LastPrice",
        "close_on_trigger": True,
        "position_idx": 2,
    }


def _split_intent(*, cycle_index: int, stage_index: int, qty: float) -> StrategyIntent:
    return StrategyIntent(
        side="short",
        qty=qty,
        purpose=f"CYCLE_{cycle_index}_SHORT_REDUCE",
        order_type="Market",
        reduce_only=True,
        trigger_price=0.589,
        trigger_direction=2,
        trigger_by="LastPrice",
        close_on_trigger=True,
        position_idx=2,
        metadata=_split_metadata(cycle_index=cycle_index, stage_index=stage_index),
    )


def _runtime() -> GenericHedgeRuntime:
    strategy = FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="long_bot_1",
            strategy_side="long",
            symbol="APTUSDT",
            restart=False,
            qty_step=0.01,
            min_order_qty=0.01,
            min_notional_usdt=5.0,
            price_tick_size=0.0001,
        )
    )
    config = GenericRuntimeConfig(
        api_key="k",
        secret_key="s",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        bot_name="long_bot_1",
    )
    order_manager = mock.Mock()
    order_manager.normalize_qty.side_effect = lambda symbol, qty, category: qty
    order_manager.ensure_max_leverage.return_value = True
    order_manager.submit_order.return_value = {"orderId": "ex-1", "orderLinkId": "cid-1"}
    runtime = GenericHedgeRuntime(config, strategy, order_manager=order_manager)
    runtime.runtime_state.last_snapshot = HedgeSnapshot(
        symbol="APTUSDT",
        current_price=0.60,
        long_qty=100.0,
        short_qty=50.0,
        long_avg=0.61,
        short_avg=0.613,
    )
    return runtime


class CycleSubmitIdentityTests(unittest.TestCase):
    def test_split_stages_have_distinct_identities(self) -> None:
        meta0 = _split_metadata(cycle_index=2, stage_index=0)
        meta1 = _split_metadata(cycle_index=2, stage_index=1)
        id0 = cycle_submit_identity("CYCLE_2_SHORT_REDUCE", meta0)
        id1 = cycle_submit_identity("CYCLE_2_SHORT_REDUCE", meta1)
        self.assertNotEqual(id0, id1)
        self.assertEqual(id0, ("CYCLE_2_SHORT_REDUCE", "normal_split", 2, 0, 2))


class RuntimeSplitSubmitTests(unittest.TestCase):
    def test_stage_one_submit_does_not_cancel_stage_zero(self) -> None:
        runtime = _runtime()
        snapshot = runtime.runtime_state.last_snapshot
        stage0 = ManagedOrder(
            client_order_id="fixed_cycle-cycle_2_short_reduce-split0-aaa",
            side="short",
            qty=8.91,
            purpose="CYCLE_2_SHORT_REDUCE",
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-stage0",
            status="OPEN",
            remaining_qty=8.91,
            metadata=_split_metadata(cycle_index=2, stage_index=0),
        )
        runtime.runtime_state.active_orders[stage0.client_order_id] = stage0

        cancel_mock = mock.Mock()
        runtime._cancel_open_orders_by_purpose_internal = cancel_mock
        runtime._submit_to_exchange = mock.Mock(return_value={"orderId": "ex-stage1"})

        client_id = runtime.submit_intent(
            _split_intent(cycle_index=2, stage_index=1, qty=8.92),
            snapshot,
            source="test",
        )

        cancel_mock.assert_not_called()
        self.assertIsNotNone(client_id)
        self.assertIn(stage0.client_order_id, runtime.runtime_state.active_orders)
        self.assertIn("-split1-", client_id or "")

    def test_equivalence_treats_other_split_stage_as_no_candidate(self) -> None:
        runtime = _runtime()
        stage0 = ManagedOrder(
            client_order_id="fixed_cycle-cycle_2_short_reduce-split0-aaa",
            side="short",
            qty=8.91,
            purpose="CYCLE_2_SHORT_REDUCE",
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-stage0",
            status="OPEN",
            remaining_qty=8.91,
            metadata={
                **_split_metadata(cycle_index=2, stage_index=0),
                "trigger_price": 0.589,
            },
        )
        runtime.runtime_state.active_orders[stage0.client_order_id] = stage0
        intent = _split_intent(cycle_index=2, stage_index=1, qty=8.92)

        equivalent, reason, *_ = runtime._find_equivalent_open_order(intent)

        self.assertIsNone(equivalent)
        self.assertEqual(reason, "no_candidate")

    def test_same_split_stage_trigger_change_cancels_only_matching_stage(self) -> None:
        runtime = _runtime()
        snapshot = runtime.runtime_state.last_snapshot
        stage0 = ManagedOrder(
            client_order_id="fixed_cycle-cycle_2_short_reduce-split0-aaa",
            side="short",
            qty=8.91,
            purpose="CYCLE_2_SHORT_REDUCE",
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-stage0",
            status="OPEN",
            remaining_qty=8.91,
            metadata={
                **_split_metadata(cycle_index=2, stage_index=0),
                "trigger_price": 0.580,
            },
        )
        stage1 = ManagedOrder(
            client_order_id="fixed_cycle-cycle_2_short_reduce-split1-bbb",
            side="short",
            qty=8.92,
            purpose="CYCLE_2_SHORT_REDUCE",
            price=None,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-stage1",
            status="OPEN",
            remaining_qty=8.92,
            metadata={
                **_split_metadata(cycle_index=2, stage_index=1),
                "trigger_price": 0.589,
            },
        )
        runtime.runtime_state.active_orders[stage0.client_order_id] = stage0
        runtime.runtime_state.active_orders[stage1.client_order_id] = stage1

        canceled: list[str] = []
        original_cancel = runtime.order_manager.cancel_order

        def _track_cancel(exchange_order_id: str, **kwargs):
            canceled.append(exchange_order_id)
            return True

        runtime.order_manager.cancel_order = _track_cancel
        runtime._submit_to_exchange = mock.Mock(return_value={"orderId": "ex-stage0-new"})

        intent = _split_intent(cycle_index=2, stage_index=0, qty=8.91)
        intent.metadata["replace_open_purpose"] = "CYCLE_2_SHORT_REDUCE"
        intent.trigger_price = 0.589

        runtime.submit_intent(intent, snapshot, source="test")

        self.assertEqual(canceled, ["ex-stage0"])
        self.assertIn(stage1.client_order_id, runtime.runtime_state.active_orders)

    def test_exit_cancel_guard_still_protects_long_tp_exit(self) -> None:
        runtime = _runtime()
        snapshot = runtime.runtime_state.last_snapshot
        runtime.runtime_state.last_snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.60,
            long_qty=100.0,
            short_qty=0.0,
            long_avg=0.61,
            short_avg=0.613,
        )
        long_tp = ManagedOrder(
            client_order_id="fixed_cycle-long_tp_exit-aaa",
            side="long",
            qty=100.0,
            purpose=runtime.strategy.LONG_TP_EXIT_PURPOSE,
            price=0.62,
            order_type="Market",
            reduce_only=True,
            exchange_order_id="ex-long-tp",
            status="OPEN",
            remaining_qty=100.0,
            metadata={"trigger_price": 0.62, "position_idx": 1},
        )
        runtime.runtime_state.active_orders[long_tp.client_order_id] = long_tp
        runtime.order_manager.cancel_order = mock.Mock(return_value=True)

        runtime._cancel_open_orders_by_purpose_internal(
            [runtime.strategy.LONG_TP_EXIT_PURPOSE],
            {"reason": "trigger_diff"},
        )

        runtime.order_manager.cancel_order.assert_not_called()
        self.assertIn(long_tp.client_order_id, runtime.runtime_state.active_orders)


class StrategySplitGateTests(unittest.TestCase):
    def _strategy(self) -> FixedCycleHedgeStrategy:
        return FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                restart=False,
                qty_step=0.01,
                min_order_qty=0.01,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
            )
        )

    def _gate_state(self) -> dict:
        return {
            "cycle_waiting_for_short_tp": True,
            "short_tp_pending_cycle": 2,
            "pending_short_cycle_index": 2,
            "active_cycle_index": 2,
            "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
            "next_required_purpose": "CYCLE_2_SHORT_REDUCE",
            "initial_entry_confirmed": True,
            "normal_cycle_second_leg_split_stage_count": {"2": 2},
            "normal_cycle_second_leg_split_filled_stages": {"2": []},
            "processed_cycle_purposes": ["CYCLE_2_LONG_ADD"],
            "cycle_states": {
                "2": {
                    "long_add_status": "PROCESSED",
                    "short_tp_status": "INTENT_BUILT",
                    "complete": False,
                }
            },
            "cycle_state": {
                "long_fills": {"2": {"price": 0.61, "incremental_qty": 30.0}},
                "short_fills": {},
            },
        }

    def test_gate_allows_missing_split_stage_while_stage_zero_open(self) -> None:
        strategy = self._strategy()
        runtime_state = RuntimeState(strategy_state=self._gate_state())
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.60,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=0.61,
            short_avg=0.613,
            active_orders=[
                ManagedOrder(
                    client_order_id="fixed_cycle-cycle_2_short_reduce-split0-aaa",
                    side="short",
                    qty=8.91,
                    purpose="CYCLE_2_SHORT_REDUCE",
                    price=None,
                    order_type="Market",
                    reduce_only=True,
                    exchange_order_id="ex-stage0",
                    status="OPEN",
                    remaining_qty=8.91,
                    metadata=_split_metadata(cycle_index=2, stage_index=0),
                )
            ],
        )

        allowed, reason, _ = strategy._can_submit_cycle_intent(
            runtime_state,
            snapshot,
            purpose="CYCLE_2_SHORT_REDUCE",
            cycle_index=2,
            cycle_role="short_reduce",
        )

        self.assertTrue(allowed, msg=reason)

    def test_gate_blocks_when_all_split_stages_active(self) -> None:
        strategy = self._strategy()
        runtime_state = RuntimeState(strategy_state=self._gate_state())
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.60,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=0.61,
            short_avg=0.613,
            active_orders=[
                ManagedOrder(
                    client_order_id="fixed_cycle-cycle_2_short_reduce-split0-aaa",
                    side="short",
                    qty=8.91,
                    purpose="CYCLE_2_SHORT_REDUCE",
                    price=None,
                    order_type="Market",
                    reduce_only=True,
                    exchange_order_id="ex-stage0",
                    status="OPEN",
                    remaining_qty=8.91,
                    metadata=_split_metadata(cycle_index=2, stage_index=0),
                ),
                ManagedOrder(
                    client_order_id="fixed_cycle-cycle_2_short_reduce-split1-bbb",
                    side="short",
                    qty=8.92,
                    purpose="CYCLE_2_SHORT_REDUCE",
                    price=None,
                    order_type="Market",
                    reduce_only=True,
                    exchange_order_id="ex-stage1",
                    status="OPEN",
                    remaining_qty=8.92,
                    metadata=_split_metadata(cycle_index=2, stage_index=1),
                ),
            ],
        )

        allowed, reason, _ = strategy._can_submit_cycle_intent(
            runtime_state,
            snapshot,
            purpose="CYCLE_2_SHORT_REDUCE",
            cycle_index=2,
            cycle_role="short_reduce",
        )

        self.assertFalse(allowed)
        self.assertTrue(
            "order_still_open" in reason or "slot_reserved" in reason,
            msg=reason,
        )

    def test_split_intents_do_not_carry_replace_open_purpose(self) -> None:
        strategy = self._strategy()
        runtime_state = RuntimeState(
            strategy_state={
                "normal_cycle_second_leg_split_stage_count": {},
                "normal_cycle_second_leg_split_filled_stages": {},
            }
        )
        runtime_state.instrument_rules["APTUSDT"] = {
            "min_order_qty": Decimal("0.01"),
            "min_notional": Decimal("5"),
            "qty_step": Decimal("0.01"),
            "tick_size": Decimal("0.0001"),
        }
        intents = strategy._maybe_build_normal_cycle_second_leg_split_intents(
            cycle_index=2,
            purpose="CYCLE_2_SHORT_REDUCE",
            qty=17.83,
            trigger_price=0.589,
            snapshot=HedgeSnapshot(
                symbol="APTUSDT",
                current_price=0.60,
                long_qty=100.0,
                short_qty=50.0,
                long_avg=0.61,
                short_avg=0.613,
            ),
            runtime_state=runtime_state,
            side="short",
            position_idx=2,
            trigger_direction=2,
            metadata={
                "cycle_index": 2,
                "cycle_role": "short_reduce",
                "replace_open_purpose": "CYCLE_2_SHORT_REDUCE",
            },
        )
        self.assertIsNotNone(intents)
        assert intents is not None
        self.assertTrue(all("replace_open_purpose" not in intent.metadata for intent in intents))


if __name__ == "__main__":
    unittest.main()
