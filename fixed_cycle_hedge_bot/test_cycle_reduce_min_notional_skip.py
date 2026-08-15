#!/usr/bin/env python3
"""Regression: C8 min-notional Skip, full-close ban, cover caps, illicit Flat→REFILL."""
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.cycle_sequence import STEP_WAITING_FOR_PAIR_SECOND_LEG
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, RuntimeState


def _apt_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.001"),
        "tick_size": Decimal("0.0001"),
    }


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_cycle_reduce_min_notional_skip")),
        runtime_name="test_runtime",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
    )


def _c8_followup_state(*, cycle_index: int = 8, pending_loss: float = 6.893855) -> dict:
    return {
        "cycle_waiting_for_short_tp": True,
        "short_tp_pending_cycle": cycle_index,
        "pending_short_cycle_index": cycle_index,
        "initial_short_qty": 25.431,
        "initial_long_qty": 50.862,
        "entry_reference_price": 1.5392,
        "cycle_step": STEP_WAITING_FOR_PAIR_SECOND_LEG,
        "next_required_purpose": f"CYCLE_{cycle_index}_SHORT_REDUCE",
        "active_cycle_index": cycle_index,
        "current_short_cycle_index": 0,
        "current_long_cycle_index": cycle_index,
        "processed_cycle_purposes": [f"CYCLE_{cycle_index}_LONG_ADD"],
        "initial_entry_confirmed": True,
        "pending_cycle_loss_usdt": pending_loss,
        "cycle_long_add_filled": True,
        "cycle_short_tp_filled": False,
        "cycle_states": {
            str(cycle_index): {
                "long_add_status": "PROCESSED",
                "short_tp_status": "NONE",
                "long_add_confirmed_pnl": -pending_loss,
            }
        },
        "cycle_state": {
            "symbol": "APTUSDT",
            "long_fills": {
                str(cycle_index): {
                    "price": 0.8175,
                    "incremental_qty": 9.536,
                    "closed_pnl": -pending_loss,
                    "confirmed_closed_pnl": -pending_loss,
                    "closed_pnl_ready": True,
                }
            },
            "short_fills": {},
            "long_cycle_index": cycle_index,
            "short_cycle_index": 0,
            "last_cycle_reference_price": 0.8175,
        },
    }


class C8MinNotionalSkipTests(unittest.TestCase):
    def test_a_c8_min_notional_skip_no_full_close(self) -> None:
        """C8: strategy 4.7685 below min notional → Skip, not full 19.074."""
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                restart=False,
                qty_step=0.001,
                min_order_qty=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
            )
        )
        pending_loss = 6.893855
        cycle_index = 8
        short_qty = 19.074
        runtime_state = RuntimeState(strategy_state=_c8_followup_state(pending_loss=pending_loss))
        runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.7955,
            long_qty=28.611,
            short_qty=short_qty,
            long_avg=1.5391333,
            short_avg=1.53919304,
        )
        audit_events: list[tuple[str, dict]] = []

        def _capture_audit(event, **payload):
            audit_events.append((event, dict(payload)))

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
            return_value=0.0,
        ), mock.patch.object(
            _context().audit,
            "log_event",
            side_effect=_capture_audit,
        ):
            context = _context()
            with mock.patch.object(context.audit, "log_event", side_effect=_capture_audit):
                intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)

        self.assertEqual(intents, [])
        state = runtime_state.strategy_state
        self.assertAlmostEqual(float(state["pending_cycle_loss_usdt"]), pending_loss)
        self.assertEqual(state["next_required_purpose"], f"CYCLE_{cycle_index}_SHORT_REDUCE")
        self.assertTrue(state["cycle_waiting_for_short_tp"])
        self.assertFalse(bool(state.get("cycle_short_tp_filled")))
        self.assertEqual(int(state.get("short_tp_pending_cycle") or 0), cycle_index)
        skip_events = [
            payload
            for event, payload in audit_events
            if event == "fixed_cycle_short_tp_follow_up_skip"
        ]
        self.assertGreaterEqual(len(skip_events), 1)
        self.assertEqual(skip_events[0].get("reason"), "short_reduce_qty_below_min_notional")
        self.assertEqual(skip_events[0].get("action"), "skip")
        self.assertNotIn("REFILL_SHORT", str(audit_events))

    def test_b_valid_partial_short_reduce_still_built(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                restart=False,
                qty_step=0.001,
                min_order_qty=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
            )
        )
        cycle_index = 2
        short_before = 19.093
        cycle_qty = 4.773
        runtime_state = RuntimeState(
            strategy_state=_c8_followup_state(cycle_index=cycle_index, pending_loss=0.385)
        )
        runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.88,
            long_qty=28.611,
            short_qty=short_before,
            long_avg=1.9661,
            short_avg=1.9661,
        )
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
            return_value=cycle_qty,
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, _context())
        self.assertGreaterEqual(len(intents), 1)
        total_qty = sum(float(i.qty) for i in intents)
        self.assertAlmostEqual(total_qty, cycle_qty, places=3)
        self.assertLess(total_qty, short_before)

    def test_c_silent_full_close_forbidden(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                restart=False,
                qty_step=0.001,
                min_order_qty=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
            )
        )
        short_before = 19.074
        runtime_state = RuntimeState(strategy_state=_c8_followup_state())
        runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.7955,
            long_qty=28.611,
            short_qty=short_before,
            long_avg=1.5391,
            short_avg=1.5392,
        )
        audit_events: list[tuple[str, dict]] = []
        context = _context()
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
            return_value=short_before,
        ), mock.patch.object(
            context.audit,
            "log_event",
            side_effect=lambda event, **payload: audit_events.append((event, dict(payload))),
        ):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)
        self.assertEqual(intents, [])
        reasons = [p.get("reason") for e, p in audit_events if e == "fixed_cycle_short_tp_follow_up_skip"]
        self.assertIn("short_reduce_full_close_forbidden", reasons)

    def test_e_pending_coverage_preserved_on_skip(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                qty_step=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
                target_profit_usdt=0.015,
                reduction_pct_per_fill=25,
            )
        )
        pending_loss = 6.893855
        runtime_state = RuntimeState(strategy_state=_c8_followup_state(pending_loss=pending_loss))
        runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.7955,
            long_qty=28.611,
            short_qty=19.074,
            long_avg=1.5391,
            short_avg=1.5392,
        )
        with mock.patch.object(
            strategy, "_maybe_activate_recovery_after_first_leg_fill", return_value=False
        ), mock.patch.object(
            strategy, "_can_submit_cycle_intent", return_value=(True, "ok", {})
        ), mock.patch.object(strategy, "_fixed_short_cycle_qty", return_value=0.0):
            intents = strategy._build_short_tp_follow_up(snapshot, runtime_state, _context())
        self.assertEqual(intents, [])
        self.assertAlmostEqual(
            float(runtime_state.strategy_state["pending_cycle_loss_usdt"]), pending_loss
        )
        self.assertFalse(bool(runtime_state.strategy_state.get("cycle_short_tp_filled")))

    def test_f_illicit_flat_blocks_refill(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                qty_step=0.001,
                min_notional_usdt=5.0,
            )
        )
        runtime_state = RuntimeState(
            strategy_state={
                "refill_pending": True,
                "refill_required": True,
                "cycle_block_status": "REFILL_REQUIRED",
                "initial_long_qty": 50.862,
                "initial_short_qty": 25.431,
                "last_confirmed_short_fill_purpose": "CYCLE_8_SHORT_REDUCE",
                "last_completed_purpose": "CYCLE_8_SHORT_REDUCE",
                "pending_cycle_loss_usdt": 6.89,
            }
        )
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.7955,
            long_qty=28.611,
            short_qty=0.0,
            long_avg=1.5391,
            short_avg=0.0,
        )
        blocked = strategy._block_refill_after_illicit_cycle_flat(
            snapshot,
            runtime_state,
            _context(),
            refill_payload={
                "current_price": 0.7955,
                "initial_long_qty": 50.862,
                "initial_short_qty": 25.431,
                "current_long_qty": 28.611,
                "current_short_qty": 0.0,
                "missing_long_qty": 22.251,
                "missing_short_qty": 25.431,
                "refill_long_qty": 22.251,
                "refill_short_qty": 25.431,
            },
        )
        self.assertTrue(blocked)
        self.assertTrue(runtime_state.strategy_state.get("refill_blocked_illicit_cycle_flat"))

    def test_f_legitimate_partial_does_not_block_refill(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                qty_step=0.001,
                min_notional_usdt=5.0,
            )
        )
        runtime_state = RuntimeState(
            strategy_state={
                "last_confirmed_short_fill_purpose": "CYCLE_2_SHORT_REDUCE",
                "last_completed_purpose": "CYCLE_2_SHORT_REDUCE",
            }
        )
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.87,
            long_qty=28.611,
            short_qty=14.32,
            long_avg=1.9661,
            short_avg=1.9661,
        )
        blocked = strategy._block_refill_after_illicit_cycle_flat(
            snapshot,
            runtime_state,
            _context(),
            refill_payload={
                "current_price": 1.87,
                "initial_long_qty": 50.862,
                "initial_short_qty": 25.431,
                "current_long_qty": 28.611,
                "current_short_qty": 14.32,
                "missing_long_qty": 22.251,
                "missing_short_qty": 11.111,
                "refill_long_qty": 22.251,
                "refill_short_qty": 11.111,
            },
        )
        self.assertFalse(blocked)

    def test_g_cover_invariant_never_reaches_full_short(self) -> None:
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="long_bot_1",
                strategy_side="long",
                symbol="APTUSDT",
                qty_step=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
            )
        )
        runtime_state = RuntimeState(strategy_state={"initial_short_qty": 19.074})
        runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.8,
            long_qty=28.611,
            short_qty=19.074,
            long_avg=1.5391,
            short_avg=1.5392,
        )
        # Huge required_net would previously push qty toward full inventory.
        trigger, qty, adjustment = strategy._enforce_short_reduce_loss_cover_invariant(
            trigger_price=0.8,
            short_qty=4.7685,
            short_entry_price=1.5392,
            required_net=50.0,
            fee_rate=0.00055,
            runtime_state=runtime_state,
            snapshot=snapshot,
            price_tick_size=0.0001,
        )
        self.assertLess(qty, 19.074 - 0.0005)
        self.assertGreater(qty, 0.0)

    def test_g_long_cover_invariant_mirror_cap(self) -> None:
        strategy = ShortFixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(
                bot_name="short_bot_1",
                strategy_side="short",
                symbol="APTUSDT",
                qty_step=0.001,
                min_notional_usdt=5.0,
                price_tick_size=0.0001,
            )
        )
        runtime_state = RuntimeState(strategy_state={"initial_long_qty": 38.147})
        runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.2,
            long_qty=38.147,
            short_qty=25.431,
            long_avg=1.0,
            short_avg=1.0,
        )
        trigger, qty, adjustment = strategy._enforce_long_reduce_loss_cover_invariant(
            trigger_price=1.2,
            long_qty=9.536,
            long_entry_price=1.0,
            required_net=50.0,
            fee_rate=0.00055,
            runtime_state=runtime_state,
            snapshot=snapshot,
            price_tick_size=0.0001,
        )
        self.assertLess(qty, 38.147 - 0.0005)

    def test_h_mid_pair_ratio_1_5_is_valid_state(self) -> None:
        """Documented intermediate ratio ~1.5 must not be treated as error by refill guard."""
        long_qty = 38.147
        short_qty = 25.431
        ratio = long_qty / short_qty
        self.assertAlmostEqual(ratio, 1.5, places=3)
        strategy = FixedCycleHedgeStrategy(
            FixedCycleHedgeConfig(bot_name="long_bot_1", strategy_side="long", symbol="APTUSDT")
        )
        runtime_state = RuntimeState(
            strategy_state={"last_completed_purpose": "CYCLE_1_LONG_ADD"}
        )
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.9,
            long_qty=long_qty,
            short_qty=short_qty,
            long_avg=1.9661,
            short_avg=1.9661,
        )
        blocked = strategy._block_refill_after_illicit_cycle_flat(
            snapshot,
            runtime_state,
            _context(),
            refill_payload={
                "current_long_qty": long_qty,
                "current_short_qty": short_qty,
                "initial_long_qty": 50.862,
                "initial_short_qty": 25.431,
            },
        )
        self.assertFalse(blocked)

    def test_i_relative_ladder_percentages(self) -> None:
        """Golden relative ladder from C1/C2 semantics (qty math only)."""
        l0, s0 = 50.862, 25.431
        pct = 0.25
        l1 = l0 * (1 - pct)
        s1 = s0
        self.assertAlmostEqual(l1 / s1, 1.5, places=3)
        s2 = s1 * (1 - pct)
        self.assertAlmostEqual(l1 / s2, 2.0, places=2)
        l3 = l1 * (1 - pct)
        self.assertAlmostEqual(l3 / s2, 1.5, places=2)
        s4 = s2 * (1 - pct)
        self.assertAlmostEqual(l3 / s4, 2.0, places=2)
        # refill restores initials
        self.assertAlmostEqual(l0 / s0, 2.0, places=6)


if __name__ == "__main__":
    unittest.main()
