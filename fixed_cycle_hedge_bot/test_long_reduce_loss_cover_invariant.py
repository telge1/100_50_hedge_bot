#!/usr/bin/env python3
"""Short-primary long-reduce loss cover invariant (mirror of short-reduce for long-primary)."""

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
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _jtousdt_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.1"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.1"),
        "tick_size": Decimal("0.0001"),
    }


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_long_reduce_loss_cover_invariant")),
        runtime_name="test_runtime",
        symbol="JTOUSDT",
        category="linear",
        min_order_value=5.0,
    )


def _short_primary_followup_state(
    *,
    cycle_index: int = 1,
    long_reduce_qty: float = 21.2,
    short_loss: float = 0.1275507199999988,
) -> dict:
    return {
        "cycle_waiting_for_long_reduce": True,
        "long_reduce_pending_cycle": cycle_index,
        "pending_long_cycle_index": cycle_index,
        "initial_short_qty": 84.8,
        "initial_long_qty": 67.2,
        "entry_reference_price": 0.744,
        "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
        "next_required_purpose": f"CYCLE_{cycle_index}_LONG_REDUCE",
        "active_cycle_index": cycle_index,
        "current_short_cycle_index": cycle_index,
        "current_long_cycle_index": 0,
        "processed_cycle_purposes": [f"CYCLE_{cycle_index}_SHORT_REDUCE"],
        "initial_entry_confirmed": True,
        "pending_cycle_loss_usdt": short_loss,
        "cycle_states": {
            str(cycle_index): {
                "short_reduce_status": "PROCESSED",
                "long_reduce_status": "NONE",
                "long_add_confirmed_pnl": -short_loss,
                "short_reduce_fill_price": 0.7438,
                "complete": False,
            }
        },
        "cycle_state": {
            "symbol": "JTOUSDT",
            "short_fills": {
                str(cycle_index): {
                    "price": 0.7438,
                    "incremental_qty": 21.2,
                    "closed_pnl": -short_loss,
                    "confirmed_closed_pnl": -short_loss,
                }
            },
            "long_fills": {},
            "short_cycle_index": cycle_index,
            "long_cycle_index": 0,
        },
    }


class LongReduceLossCoverInvariantTests(unittest.TestCase):
    def test_short_primary_long_reduce_meets_required_net(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="short_bot_1",
                strategy_side="short",
                symbol="JTOUSDT",
                restart=False,
                qty_step=0.1,
                min_order_qty=0.1,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
            )
        )
        cycle_index = 1
        short_loss = 0.1275507199999988
        required_net = short_loss + 0.015
        long_reduce_qty = 21.2
        long_entry = 0.744
        runtime_state = RuntimeState(
            strategy_state=_short_primary_followup_state(
                cycle_index=cycle_index,
                long_reduce_qty=long_reduce_qty,
                short_loss=short_loss,
            )
        )
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()
        snapshot = HedgeSnapshot(
            symbol="JTOUSDT",
            current_price=0.75,
            long_qty=67.2,
            short_qty=84.8,
            long_avg=long_entry,
            short_avg=0.7438,
        )

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
            "_fixed_long_cycle_qty",
            return_value=long_reduce_qty,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, _context())

        self.assertGreaterEqual(len(intents), 1)
        total_qty = sum(intent.qty for intent in intents)
        trigger_price = float(intents[0].trigger_price or 0.0)
        fee_rate = 0.00055
        expected_net = strategy._compute_long_reduce_net(
            long_entry_price=long_entry,
            trigger_price=trigger_price,
            qty=total_qty,
            fee_rate=fee_rate,
        )
        self.assertGreaterEqual(expected_net + SHORT_REDUCE_COVER_NET_EPS, required_net)
        self.assertGreater(trigger_price, long_entry)

    def test_enforce_helper_raises_trigger_before_qty(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="short_bot_1",
                strategy_side="short",
                symbol="JTOUSDT",
                price_tick_size=0.0001,
            )
        )
        runtime_state = RuntimeState(strategy_state={})
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()
        snapshot = HedgeSnapshot(
            symbol="JTOUSDT",
            current_price=0.75,
            long_qty=67.2,
            short_qty=84.8,
            long_avg=0.744,
            short_avg=0.7438,
        )
        required_net = 0.1425507199999988
        trigger, qty, adjustment = strategy._enforce_long_reduce_loss_cover_invariant(
            trigger_price=0.748,
            long_qty=21.2,
            long_entry_price=0.744,
            required_net=required_net,
            fee_rate=0.00055,
            runtime_state=runtime_state,
            snapshot=snapshot,
            price_tick_size=0.0001,
        )
        net = strategy._compute_long_reduce_net(
            long_entry_price=0.744,
            trigger_price=trigger,
            qty=qty,
            fee_rate=0.00055,
        )
        self.assertGreaterEqual(net + SHORT_REDUCE_COVER_NET_EPS, required_net)
        self.assertGreater(trigger, 0.748)
        self.assertEqual(qty, 21.2)
        self.assertTrue(adjustment.get("adjusted"))

    def test_short_primary_long_reduce_rejects_undercovered_candidate(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(symbol="JTOUSDT", price_tick_size=0.0001)
        )
        net = strategy._compute_long_reduce_net(
            long_entry_price=0.744,
            trigger_price=0.745,
            qty=21.2,
            fee_rate=0.00055,
        )
        self.assertLess(net, 0.1425507199999988)

    def test_short_primary_long_reduce_accepts_overcovered_candidate(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(symbol="JTOUSDT", price_tick_size=0.0001)
        )
        runtime_state = RuntimeState(strategy_state={})
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()
        snapshot = HedgeSnapshot(
            symbol="JTOUSDT",
            current_price=0.75,
            long_qty=67.2,
            short_qty=84.8,
            long_avg=0.744,
            short_avg=0.7438,
        )
        trigger, qty, _ = strategy._enforce_long_reduce_loss_cover_invariant(
            trigger_price=0.752,
            long_qty=21.2,
            long_entry_price=0.744,
            required_net=0.1425507199999988,
            fee_rate=0.00055,
            runtime_state=runtime_state,
            snapshot=snapshot,
            price_tick_size=0.0001,
        )
        net = strategy._compute_long_reduce_net(
            long_entry_price=0.744,
            trigger_price=trigger,
            qty=qty,
            fee_rate=0.00055,
        )
        self.assertGreaterEqual(net + SHORT_REDUCE_COVER_NET_EPS, 0.1425507199999988)

    def test_short_primary_pending_cycle_loss_cleared_after_long_reduce_cover(self) -> None:
        """Invariant enforcement ensures cover net meets pending_cycle_loss + target."""
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="short_bot_1",
                strategy_side="short",
                symbol="JTOUSDT",
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
            )
        )
        short_loss = 0.5
        runtime_state = RuntimeState(
            strategy_state=_short_primary_followup_state(short_loss=short_loss)
        )
        runtime_state.instrument_rules["JTOUSDT"] = _jtousdt_rules()
        snapshot = HedgeSnapshot(
            symbol="JTOUSDT",
            current_price=0.75,
            long_qty=67.2,
            short_qty=84.8,
            long_avg=0.744,
            short_avg=0.7438,
        )
        required_net = short_loss + 0.015

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
            "_fixed_long_cycle_qty",
            return_value=21.2,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, _context())

        total_qty = sum(intent.qty for intent in intents)
        trigger = float(intents[0].trigger_price or 0.0)
        net = strategy._compute_long_reduce_net(
            long_entry_price=0.744,
            trigger_price=trigger,
            qty=total_qty,
            fee_rate=0.00055,
        )
        self.assertGreaterEqual(net + SHORT_REDUCE_COVER_NET_EPS, required_net)
        metadata = intents[0].metadata or {}
        required_in_metadata = float(
            metadata.get("required_profit_to_cover_loss")
            or metadata.get("stage_required_net_total")
            or metadata.get("required_net")
            or 0.0
        )
        self.assertAlmostEqual(required_in_metadata, required_net, places=6)


if __name__ == "__main__":
    unittest.main()
