#!/usr/bin/env python3
"""Preventive next-cycle min-notional refill: projection, trigger, cancel, recheck, rebuild."""
from __future__ import annotations

import logging
import unittest
from decimal import Decimal
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, ManagedOrder, RuntimeState


def _apt_rules() -> dict[str, Decimal]:
    return {
        "min_order_qty": Decimal("0.001"),
        "min_notional": Decimal("5"),
        "qty_step": Decimal("0.001"),
        "tick_size": Decimal("0.0001"),
    }


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_preventive_next_cycle_min_notional_refill")),
        runtime_name="test_runtime",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
    )


def _long_strategy() -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
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
            next_cycle_min_notional_safety_factor=1.0,
        )
    )


def _short_strategy() -> ShortFixedCycleHedgeStrategy:
    return ShortFixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            bot_name="short_bot_1",
            strategy_side="short",
            symbol="APTUSDT",
            restart=False,
            qty_step=0.001,
            min_order_qty=0.001,
            min_notional_usdt=5.0,
            price_tick_size=0.0001,
            target_profit_usdt=0.015,
            reduction_pct_per_fill=25,
            next_cycle_min_notional_safety_factor=1.0,
        )
    )


def _c7_runtime(*, price: float = 0.8216, short_qty: float = 19.074, long_qty: float = 38.147) -> tuple[RuntimeState, HedgeSnapshot]:
    runtime_state = RuntimeState(
        strategy_state={
            "cycle_completed_count": 7,
            "cycle_pair_count": 7,
            "last_refill_completed_cycle_index": 6,
            "last_refill_completed_cycle_pair_count": 6,
            "initial_long_qty": 50.862,
            "initial_short_qty": 25.431,
            "initial_entry_confirmed": True,
            "bot_state": "RUNNING",
        }
    )
    runtime_state.instrument_rules["APTUSDT"] = _apt_rules()
    snapshot = HedgeSnapshot(
        symbol="APTUSDT",
        current_price=price,
        long_qty=long_qty,
        short_qty=short_qty,
        long_avg=1.5391,
        short_avg=1.5392,
    )
    runtime_state.last_snapshot = snapshot
    return runtime_state, snapshot


def _active_order(purpose: str, client_id: str, exchange_id: str) -> ManagedOrder:
    return ManagedOrder(
        client_order_id=client_id,
        exchange_order_id=exchange_id,
        purpose=purpose,
        side="Sell",
        qty=1.0,
        price=1.0,
        status="OPEN",
        remaining_qty=1.0,
        order_type="Limit",
        reduce_only=True,
        metadata={},
    )


class PreventiveNextCycleMinNotionalRefillTests(unittest.TestCase):
    def test_a_c7_projection_triggers_preventive_refill(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime()
        projection = strategy._project_next_cycle_second_leg_min_notional(snapshot, runtime_state)
        self.assertFalse(projection["valid"])
        self.assertAlmostEqual(projection["projected_notional"], 3.9173888, places=5)
        self.assertFalse(projection["regular_refill_due"])

        strategy._decide_refill_after_cycle_completion(
            runtime_state,
            cycle_index=7,
            trigger_purpose="CYCLE_7_SHORT_REDUCE",
        )
        state = runtime_state.strategy_state
        self.assertTrue(state.get("refill_required"))
        self.assertTrue(state.get("refill_pending"))
        self.assertEqual(state.get("bot_state"), strategy.STATE_REFILL_PENDING)
        self.assertIn("preventive_next_cycle_min_notional", str(state.get("refill_trigger_reason")))
        self.assertEqual(int(state.get("preventive_min_notional_refill_for_cycle") or 0), 7)
        self.assertTrue(strategy._cycle_build_block_active(state))

    def test_b_old_cycle_and_exit_orders_cancelled_on_refill_path(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime()
        strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=7)
        state = runtime_state.strategy_state
        self.assertTrue(state.get("refill_exit_orders_cancel_required"))

        for purpose, cid, eid in (
            ("CYCLE_8_LONG_ADD", "c1", "e1"),
            ("CYCLE_7_SHORT_REDUCE", "c2", "e2"),
            ("LONG_TP_EXIT", "c3", "e3"),
            ("SHORT_SL_EXIT", "c4", "e4"),
        ):
            runtime_state.active_orders[cid] = _active_order(purpose, cid, eid)

        self.assertTrue(strategy._is_refill_stale_cycle_reduce_purpose("CYCLE_8_LONG_ADD"))
        self.assertTrue(strategy._is_refill_stale_cycle_reduce_purpose("CYCLE_7_SHORT_REDUCE"))

        cancel_calls: list[str] = []

        class _OM:
            def cancel_order(self, exchange_order_id, symbol=None, category=None):
                cancel_calls.append(str(exchange_order_id))
                return True

        context = _context()
        context.order_manager = _OM()
        ok = strategy._cancel_refill_exit_orders(runtime_state, context, state)
        self.assertTrue(ok)
        self.assertEqual(sorted(cancel_calls), ["e1", "e2", "e3", "e4"])
        self.assertFalse(runtime_state.active_orders)
        self.assertFalse(state.get("refill_exit_orders_cancel_required"))

    def test_c_no_cycle_or_exit_rebuild_while_refill_active(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime()
        strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=7)
        context = _context()
        exit_intents = strategy._build_exit_intents(
            snapshot,
            runtime_state,
            current_cycle=8,
            break_even_price=1.0,
            tp_price=1.0,
            hard_stop_active=False,
            context=context,
        )
        self.assertEqual(exit_intents, [])
        follow_up = strategy._build_short_tp_follow_up(snapshot, runtime_state, context)
        self.assertEqual(follow_up, [])
        allowed, reason, _ = strategy._can_submit_cycle_intent(
            runtime_state,
            snapshot,
            purpose="CYCLE_8_LONG_ADD",
            cycle_index=8,
            cycle_role="long_add",
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, "refill_required")

    def test_d_recheck_valid_after_refill(self) -> None:
        strategy = _long_strategy()
        runtime_state, _ = _c7_runtime()
        runtime_state.strategy_state["preventive_min_notional_refill_for_cycle"] = 7
        post = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.8216,
            long_qty=50.862,
            short_qty=25.431,
            long_avg=1.3597,
            short_avg=1.3598,
        )
        runtime_state.last_snapshot = post
        strategy._complete_refill(runtime_state, None, completion_reason="test")
        state = runtime_state.strategy_state
        self.assertFalse(state.get("next_cycle_min_notional_blocked"))
        self.assertEqual(state.get("next_required_purpose"), "CYCLE_8_LONG_ADD")
        self.assertEqual(int(state.get("last_refill_completed_cycle_index") or 0), 7)
        self.assertTrue(state.get("force_exit_rebuild"))
        self.assertTrue(state.get("post_refill_structure_rebuild_required"))
        projection = strategy._project_next_cycle_second_leg_min_notional(post, runtime_state)
        self.assertTrue(projection["valid"])
        self.assertGreaterEqual(projection["projected_notional"], 5.0)

    def test_e_f_post_refill_uses_new_qty_and_avg_flags(self) -> None:
        strategy = _long_strategy()
        runtime_state, _ = _c7_runtime()
        post = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.8216,
            long_qty=50.862,
            short_qty=25.431,
            long_avg=1.3597,
            short_avg=1.3598,
        )
        runtime_state.last_snapshot = post
        strategy._complete_refill(runtime_state, None, completion_reason="test")
        state = runtime_state.strategy_state
        # New first-leg qty from refilled long inventory.
        first_leg_qty = strategy._fixed_long_cycle_qty(
            float(state["initial_long_qty"]),
            post.long_qty,
            post.current_price,
            runtime_state=runtime_state,
        )
        self.assertAlmostEqual(first_leg_qty, 12.715, places=3)
        residual = strategy._refill_residual_snapshot_payload(post, runtime_state)
        self.assertEqual(residual["missing_long_qty"], 0.0)
        self.assertEqual(residual["missing_short_qty"], 0.0)
        self.assertTrue(state.get("exit_rebuild_allowed"))
        self.assertFalse(state.get("exit_locked"))

    def test_g_h_pair_and_preventive_merge_single_refill(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime()
        # Even-cycle boundary: regular refill due.
        runtime_state.strategy_state["cycle_completed_count"] = 6
        runtime_state.strategy_state["cycle_pair_count"] = 6
        runtime_state.strategy_state["last_refill_completed_cycle_index"] = 4
        runtime_state.strategy_state["last_refill_completed_cycle_pair_count"] = 4
        # Make projection invalid without regular refill inventory.
        snapshot.short_qty = 14.306
        snapshot.current_price = 1.1896
        enter_calls = {"n": 0}
        original = strategy._enter_refill_mode

        def _counting_enter(*args, **kwargs):
            enter_calls["n"] += 1
            return original(*args, **kwargs)

        with mock.patch.object(strategy, "_enter_refill_mode", side_effect=_counting_enter):
            strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=6)
            # Second decide while active must merge, not re-enter.
            strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=6)
        self.assertEqual(enter_calls["n"], 1)
        reason = str(runtime_state.strategy_state.get("refill_trigger_reason") or "")
        self.assertIn("preventive_next_cycle_min_notional", reason)
        self.assertIn("short_reduce_completion", reason)

    def test_i_j_block_after_refill_still_invalid_no_loop(self) -> None:
        strategy = _long_strategy()
        runtime_state, _ = _c7_runtime()
        runtime_state.strategy_state["preventive_min_notional_refill_for_cycle"] = 7
        low = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=0.70,
            long_qty=50.862,
            short_qty=25.431,
            long_avg=1.0,
            short_avg=1.0,
        )
        runtime_state.last_snapshot = low
        strategy._complete_refill(runtime_state, None, completion_reason="low_px")
        state = runtime_state.strategy_state
        self.assertTrue(state.get("next_cycle_min_notional_blocked"))
        self.assertEqual(state.get("cycle_block_status"), "NEXT_CYCLE_MIN_NOTIONAL_BLOCKED")
        self.assertIsNone(state.get("next_required_purpose"))
        self.assertFalse(state.get("force_exit_rebuild"))

        enter_calls = {"n": 0}
        with mock.patch.object(
            strategy,
            "_enter_refill_mode",
            side_effect=lambda *a, **k: enter_calls.__setitem__("n", enter_calls["n"] + 1),
        ):
            for _ in range(5):
                strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=7)
                strategy._maybe_clear_next_cycle_min_notional_block(low, runtime_state)
        self.assertEqual(enter_calls["n"], 0)
        self.assertTrue(strategy._next_cycle_min_notional_block_active(state))
        context = _context()
        self.assertEqual(
            strategy._build_exit_intents(
                low,
                runtime_state,
                current_cycle=8,
                break_even_price=1.0,
                tp_price=1.0,
                hard_stop_active=False,
                context=context,
            ),
            [],
        )

    def test_k_short_bot_projects_long_coverage_side(self) -> None:
        strategy = _short_strategy()
        runtime_state, snapshot = _c7_runtime(long_qty=19.074, short_qty=38.147)
        projection = strategy._project_next_cycle_second_leg_min_notional(snapshot, runtime_state)
        self.assertEqual(projection["position_side"], "long")
        self.assertFalse(projection["valid"])
        strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=7)
        self.assertTrue(runtime_state.strategy_state.get("refill_required"))
        self.assertIn(
            "preventive_next_cycle_min_notional",
            str(runtime_state.strategy_state.get("refill_trigger_reason")),
        )

    def test_l_c1_to_c5_no_preventive_refill(self) -> None:
        strategy = _long_strategy()
        # (completed, last_refill, short_after, price) — odd completions with valid next second-leg
        cases = [
            (1, 0, 19.093, 1.9376),
            (3, 2, 19.079, 1.8090),
            (5, 4, 19.074, 1.5074),
        ]
        for completed, last_refill, short_qty, price in cases:
            runtime_state, snapshot = _c7_runtime(price=price, short_qty=short_qty)
            runtime_state.strategy_state["cycle_completed_count"] = completed
            runtime_state.strategy_state["cycle_pair_count"] = completed
            runtime_state.strategy_state["last_refill_completed_cycle_index"] = last_refill
            runtime_state.strategy_state["last_refill_completed_cycle_pair_count"] = last_refill
            projection = strategy._project_next_cycle_second_leg_min_notional(snapshot, runtime_state)
            self.assertTrue(projection["valid"], msg=f"cycle {completed}: {projection}")
            strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=completed)
            self.assertFalse(
                runtime_state.strategy_state.get("refill_required"),
                msg=f"unexpected refill after cycle {completed}",
            )

    def test_m_c8_full_close_safety_still_skips(self) -> None:
        # Reuse existing safety: under-min short follow-up must Skip, not full close.
        from fixed_cycle_hedge_bot.test_cycle_reduce_min_notional_skip import (
            C8MinNotionalSkipTests,
        )

        C8MinNotionalSkipTests().test_a_c8_min_notional_skip_no_full_close()

    def test_n_refill_intent_path_uses_existing_machine(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime()
        strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=7)
        context = _context()
        cancel_ok = {"called": False}

        def _fake_cancel(rs, ctx, st):
            cancel_ok["called"] = True
            st["refill_exit_orders_cancel_required"] = False
            return True

        with mock.patch.object(strategy, "_cancel_refill_exit_orders", side_effect=_fake_cancel):
            with mock.patch.object(strategy, "_reconcile_refill_gate_state", return_value={
                "active_refill_orders_count": 0,
                "stale_detected": False,
            }):
                intents = strategy._build_entry_intents(snapshot, runtime_state, context)
        self.assertTrue(cancel_ok["called"])
        purposes = {intent.purpose for intent in intents}
        self.assertIn("REFILL_LONG", purposes)
        self.assertIn("REFILL_SHORT", purposes)

    def test_b_projection_above_minimum_continues(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime(price=1.20, short_qty=19.074)
        projection = strategy._project_next_cycle_second_leg_min_notional(snapshot, runtime_state)
        self.assertTrue(projection["valid"])
        strategy._decide_refill_after_cycle_completion(runtime_state, cycle_index=7)
        self.assertFalse(runtime_state.strategy_state.get("refill_required"))

    def test_safety_factor_m0_boundary(self) -> None:
        strategy = _long_strategy()
        runtime_state, snapshot = _c7_runtime(price=5.0 / 4.768, short_qty=19.074)
        projection = strategy._project_next_cycle_second_leg_min_notional(snapshot, runtime_state)
        self.assertTrue(projection["valid"])
        runtime_state2, snapshot2 = _c7_runtime(price=4.99 / 4.768, short_qty=19.074)
        projection2 = strategy._project_next_cycle_second_leg_min_notional(snapshot2, runtime_state2)
        self.assertFalse(projection2["valid"])


if __name__ == "__main__":
    unittest.main()
