#!/usr/bin/env python3
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    SHORT_REDUCE_COVER_NET_EPS,
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _ena_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.001"),
        "tick_size": Decimal("0.0001"),
    }


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_short_reduce_loss_cover_invariant")),
        runtime_name="test_runtime",
        symbol="ENAUSDT",
        category="linear",
        min_order_value=5.0,
    )


class ShortReduceLossCoverInvariantTests(unittest.TestCase):
    def test_ena_like_follow_up_meets_required_net(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="ENAUSDT",
                restart=False,
                qty_step=0.001,
                min_order_qty=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
            )
        )
        cycle_index = 1
        short_entry = 0.08624
        long_loss = 0.1275507199999988
        required_net = long_loss + 0.015
        short_tp_qty = 72.472
        short_qty = 289.888
        runtime_state = RuntimeState(
            strategy_state={
                "cycle_waiting_for_short_tp": True,
                "short_tp_pending_cycle": cycle_index,
                "pending_short_cycle_index": cycle_index,
                "initial_short_qty": short_qty,
                "initial_long_qty": short_qty,
                "entry_reference_price": 0.0858,
                "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
                "next_required_purpose": "CYCLE_1_SHORT_REDUCE",
                "active_cycle_index": cycle_index,
                "current_short_cycle_index": 0,
                "current_long_cycle_index": cycle_index,
                "processed_cycle_purposes": ["CYCLE_1_LONG_ADD"],
                "initial_entry_confirmed": True,
                "pending_cycle_loss_usdt": long_loss,
                "cycle_states": {
                    str(cycle_index): {
                        "long_add_status": "PROCESSED",
                        "short_tp_status": "NONE",
                        "short_tp_qty": short_tp_qty,
                        "long_add_confirmed_pnl": -long_loss,
                        "complete": False,
                    }
                },
                "cycle_state": {
                    "symbol": "ENAUSDT",
                    "long_fills": {
                        str(cycle_index): {
                            "price": 0.0858,
                            "incremental_qty": short_qty,
                            "closed_pnl": -long_loss,
                            "confirmed_closed_pnl": -long_loss,
                        }
                    },
                    "short_fills": {},
                    "long_cycle_index": cycle_index,
                    "short_cycle_index": 0,
                },
            }
        )
        runtime_state.instrument_rules["ENAUSDT"] = _ena_rules()
        snapshot = HedgeSnapshot(
            symbol="ENAUSDT",
            current_price=0.087,
            long_qty=short_qty,
            short_qty=short_qty,
            long_avg=0.0858,
            short_avg=short_entry,
        )
        strategy_logs: list[tuple[str, dict]] = []

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=lambda event, payload: strategy_logs.append((event, dict(payload))),
        ), mock.patch.object(
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
            return_value=short_tp_qty,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, _context())

        self.assertGreaterEqual(len(intents), 1)
        total_qty = sum(intent.qty for intent in intents)
        trigger_price = intents[0].trigger_price
        self.assertIsNotNone(trigger_price)
        assert trigger_price is not None
        fee_rate = 0.00055
        expected_net = strategy._compute_short_reduce_net(
            short_entry_price=short_entry,
            trigger_price=float(trigger_price),
            qty=total_qty,
            fee_rate=fee_rate,
        )
        self.assertGreaterEqual(
            expected_net + SHORT_REDUCE_COVER_NET_EPS,
            required_net,
        )
        self.assertLessEqual(float(trigger_price), 0.0842)
        metadata = intents[0].metadata or {}
        if metadata.get("short_reduce_loss_cover_invariant_adjusted"):
            adjusted_events = [
                payload
                for event, payload in strategy_logs
                if event == "fixed_cycle_short_reduce_loss_cover_invariant_adjusted"
            ]
            self.assertEqual(len(adjusted_events), 1)
            self.assertTrue(adjusted_events[0].get("adjusted"))

    def test_enforce_helper_deepens_trigger_before_qty(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="ENAUSDT",
                price_tick_size=0.0001,
            )
        )
        runtime_state = RuntimeState(strategy_state={})
        runtime_state.instrument_rules["ENAUSDT"] = _ena_rules()
        snapshot = HedgeSnapshot(
            symbol="ENAUSDT",
            current_price=0.087,
            long_qty=289.888,
            short_qty=289.888,
            long_avg=0.0858,
            short_avg=0.08624,
        )
        required_net = 0.1425507199999988
        trigger, qty, adjustment = strategy._enforce_short_reduce_loss_cover_invariant(
            trigger_price=0.085,
            short_qty=72.472,
            short_entry_price=0.08624,
            required_net=required_net,
            fee_rate=0.00055,
            runtime_state=runtime_state,
            snapshot=snapshot,
            price_tick_size=0.0001,
        )
        net = strategy._compute_short_reduce_net(
            short_entry_price=0.08624,
            trigger_price=trigger,
            qty=qty,
            fee_rate=0.00055,
        )
        self.assertGreaterEqual(net + SHORT_REDUCE_COVER_NET_EPS, required_net)
        self.assertLess(trigger, 0.085)
        self.assertEqual(qty, 72.472)
        self.assertTrue(adjustment.get("adjusted"))

    def test_rounded_up_trigger_is_repaired_before_submit(self) -> None:
        """Simulate HALF_UP normalize leaving trigger too shallow for required_net."""
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="ENAUSDT",
                restart=False,
                qty_step=0.001,
                min_order_qty=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
                short_tp_min_threshold_pct_after_long_reduce=0.01,
            )
        )
        cycle_index = 1
        short_entry = 0.08624
        long_loss = 0.1275507199999988
        required_net = long_loss + 0.015
        short_tp_qty = 72.472
        short_qty = 289.888
        runtime_state = RuntimeState(
            strategy_state={
                "cycle_waiting_for_short_tp": True,
                "short_tp_pending_cycle": cycle_index,
                "pending_short_cycle_index": cycle_index,
                "initial_short_qty": short_qty,
                "initial_long_qty": short_qty,
                "entry_reference_price": 0.0858,
                "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
                "next_required_purpose": "CYCLE_1_SHORT_REDUCE",
                "active_cycle_index": cycle_index,
                "current_short_cycle_index": 0,
                "current_long_cycle_index": cycle_index,
                "processed_cycle_purposes": ["CYCLE_1_LONG_ADD"],
                "initial_entry_confirmed": True,
                "pending_cycle_loss_usdt": long_loss,
                "cycle_states": {
                    str(cycle_index): {
                        "long_add_status": "PROCESSED",
                        "short_tp_status": "NONE",
                        "short_tp_qty": short_tp_qty,
                        "long_add_confirmed_pnl": -long_loss,
                        "complete": False,
                    }
                },
                "cycle_state": {
                    "symbol": "ENAUSDT",
                    "long_fills": {
                        str(cycle_index): {
                            "price": 0.0858,
                            "incremental_qty": short_qty,
                            "closed_pnl": -long_loss,
                            "confirmed_closed_pnl": -long_loss,
                        }
                    },
                    "short_fills": {},
                    "long_cycle_index": cycle_index,
                    "short_cycle_index": 0,
                },
            }
        )
        runtime_state.instrument_rules["ENAUSDT"] = _ena_rules()
        snapshot = HedgeSnapshot(
            symbol="ENAUSDT",
            current_price=0.087,
            long_qty=short_qty,
            short_qty=short_qty,
            long_avg=0.0858,
            short_avg=short_entry,
        )

        real_normalize = strategy._normalize_price

        def _round_up_short_reduce_trigger(price: float, runtime_state: RuntimeState | None = None) -> float:
            normalized = real_normalize(price, runtime_state)
            if 0.084 <= normalized <= 0.0843:
                return 0.085
            return normalized

        with mock.patch(
            "fixed_cycle_hedge_bot.fixed_cycle_strategy._log_event",
            side_effect=lambda event, payload: None,
        ), mock.patch.object(
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
            return_value=short_tp_qty,
        ), mock.patch.object(
            strategy,
            "_normalize_price",
            side_effect=_round_up_short_reduce_trigger,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, _context())

        self.assertGreaterEqual(len(intents), 1)
        trigger_price = float(intents[0].trigger_price or 0.0)
        total_qty = sum(intent.qty for intent in intents)
        net = strategy._compute_short_reduce_net(
            short_entry_price=short_entry,
            trigger_price=trigger_price,
            qty=total_qty,
            fee_rate=0.00055,
        )
        self.assertGreaterEqual(net + SHORT_REDUCE_COVER_NET_EPS, required_net)
        self.assertLess(trigger_price, 0.085)
        metadata = intents[0].metadata or {}
        self.assertTrue(metadata.get("short_reduce_loss_cover_invariant_adjusted"))


if __name__ == "__main__":
    unittest.main()
