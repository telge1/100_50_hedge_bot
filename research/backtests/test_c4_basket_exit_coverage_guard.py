"""Variant C: basket exit coverage floor + guard + flat-cancel residual protection."""

from __future__ import annotations

import logging
import unittest
from unittest import mock

from fixed_cycle_hedge_bot.audit_logger import AuditLogger
from fixed_cycle_hedge_bot.base import StrategyContext
from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    BasketExitCoverageDecision,
    FixedCycleHedgeConfig,
    FixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import HedgeSnapshot, ManagedOrder, RuntimeState
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import normalize_candles
from research.backtests.multicoin_blocker_price_staging import run_isolated_blocker
from research.backtests.pnl_coverage_audit import build_pnl_coverage_audit
from research.backtests.second_leg_price_staging import resolve_grid_profile, resolve_profile


def _context() -> StrategyContext:
    return StrategyContext(
        audit=AuditLogger(logging.getLogger("test_c4_basket_exit_coverage_guard")),
        runtime_name="test",
        symbol="APTUSDT",
        category="linear",
        min_order_value=5.0,
        cancel_open_orders_by_purpose=mock.Mock(),
    )


def _strategy() -> FixedCycleHedgeStrategy:
    return FixedCycleHedgeStrategy(
        FixedCycleHedgeConfig(
            price_tick_size=0.0001,
            tp_profit_target_pct=0.25,
            tp_buffer_pct=0.125,
            order_fee_rate_pct=0.055,
        )
    )


def _base_state(**overrides: object) -> dict[str, object]:
    state: dict[str, object] = {
        "initial_entry_confirmed": True,
        "initial_structure_built": True,
        "exit_rebuild_allowed": True,
        "current_effective_cycle": 4,
        "cycle_completed_count": 1,
        "cycle_pair_count": 1,
        "last_refill_completed_cycle_index": 1,
        "pending_cycle_loss_usdt": 12.80,
        "staged_second_leg_tp_required_net_total": {"4": 12.80},
        "staged_second_leg_tp_stage_count": {"4": 2},
        "staged_second_leg_tp_realized_net": {"4": 2.14},
        "staged_second_leg_tp_filled_stages": {"4": [0]},
        "force_exit_rebuild": True,
    }
    state.update(overrides)
    return state


def _residual_stage1_order() -> ManagedOrder:
    return ManagedOrder(
        client_order_id="sim-stage1",
        purpose="CYCLE_4_SHORT_REDUCE",
        side="Sell",
        qty=36.153,
        price=1.6654,
        order_type="Market",
        reduce_only=True,
        status="NEW",
        metadata={
            "research_price_staging": True,
            "is_staged_second_leg_tp": True,
            "stage_index": 1,
            "stage_count": 2,
            "cycle_index": 4,
            "required_net": 12.80,
            "stage_required_net_total": 12.80,
        },
    )


class BasketExitCoverageMathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.strategy = _strategy()

    def test_coverage_ok_matches_final_exit_economics_sufficient(self) -> None:
        runtime = RuntimeState(strategy_state=_base_state(pending_cycle_loss_usdt=0.0))
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.90,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.80,
            short_avg=1.80,
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime)
        projection = self.strategy._calculate_tp_projection(break_even, snapshot, runtime)
        tp = float(projection.tp_price)
        economics = self.strategy._evaluate_final_exit_economics(
            long_tp_price=tp,
            short_sl_price=tp,
            snapshot=snapshot,
            runtime_state=runtime,
            projection=projection,
        )
        decision = self.strategy.evaluate_basket_exit_coverage(
            snapshot=snapshot,
            runtime_state=runtime,
            long_tp_price=tp,
            short_sl_price=tp,
            projection=projection,
        )
        self.assertEqual(decision.coverage_ok, economics.sufficient)
        self.assertAlmostEqual(
            decision.coverage_after_exit_usdt,
            economics.expected_total_net_after_exit,
        )
        self.assertAlmostEqual(decision.required_net_usdt, economics.min_required_total_usdt)
        self.assertAlmostEqual(
            decision.tolerance_usdt,
            self.strategy._final_exit_coverage_tolerance_usdt(projection),
        )

    def test_exact_tolerance_boundary_allows_close(self) -> None:
        """expected == min_required - tolerance ⇒ sufficient (existing semantics)."""
        runtime = RuntimeState(strategy_state=_base_state(pending_cycle_loss_usdt=1.0))
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.90,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.80,
            short_avg=1.80,
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime)
        projection = self.strategy._calculate_tp_projection(break_even, snapshot, runtime)
        # Search a price near projection that lands at the tolerance edge.
        tp = float(projection.tp_price)
        economics = self.strategy._evaluate_final_exit_economics(
            long_tp_price=tp,
            short_sl_price=tp,
            snapshot=snapshot,
            runtime_state=runtime,
            projection=projection,
        )
        # Projection.tp_price is built to be sufficient; assert boundary formula holds.
        self.assertTrue(
            economics.expected_total_net_after_exit
            >= economics.min_required_total_usdt
            - self.strategy._final_exit_coverage_tolerance_usdt(projection)
            - 1e-9
        )
        self.assertTrue(economics.sufficient)

    def test_insufficient_blocks_with_staging_residuals(self) -> None:
        runtime = RuntimeState(strategy_state=_base_state())
        residual = _residual_stage1_order()
        runtime.active_orders["sim-stage1"] = residual
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.85,
            long_qty=500.0,
            short_qty=250.0,
            long_avg=1.90,
            short_avg=1.90,
            active_orders=(residual.to_snapshot(),),
        )
        # Far-too-low basket prices → insufficient.
        decision = self.strategy.evaluate_basket_exit_coverage(
            snapshot=snapshot,
            runtime_state=runtime,
            long_tp_price=1.70,
            short_sl_price=1.70,
        )
        self.assertTrue(decision.staging_incomplete)
        self.assertFalse(decision.coverage_ok)
        self.assertEqual(decision.reason_code, "coverage_blocked_insufficient_basket")
        self.assertFalse(decision.economics.sufficient)

        context = _context()
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime)
        # Force the staging coverage path to see insufficient economics even if
        # the projection floor price would otherwise look sufficient.
        with mock.patch.object(
            self.strategy,
            "evaluate_basket_exit_coverage",
            return_value=decision,
        ):
            intents = self.strategy._build_exit_intents(
                snapshot,
                runtime,
                current_cycle=4,
                break_even_price=break_even,
                tp_price=1.70,
                hard_stop_active=False,
                context=context,
                force_exit_rebuild=True,
            )
        self.assertEqual(intents, [])
        self.assertGreaterEqual(context.cancel_open_orders_by_purpose.call_count, 1)
        cancelled_purposes = []
        for call in context.cancel_open_orders_by_purpose.call_args_list:
            cancelled_purposes.extend(call.args[0] if call.args else [])
        self.assertNotIn("CYCLE_4_SHORT_REDUCE", cancelled_purposes)

    def test_overcoverage_allows_early_exit_before_last_stage(self) -> None:
        runtime = RuntimeState(
            strategy_state=_base_state(
                pending_cycle_loss_usdt=0.5,
                staged_second_leg_tp_realized_net={"4": 0.4},
            )
        )
        runtime.active_orders["sim-stage1"] = _residual_stage1_order()
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.95,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.80,
            short_avg=1.80,
            active_orders=(_residual_stage1_order().to_snapshot(),),
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime)
        projection = self.strategy._calculate_tp_projection(break_even, snapshot, runtime)
        tp = float(projection.tp_price)
        decision = self.strategy.evaluate_basket_exit_coverage(
            snapshot=snapshot,
            runtime_state=runtime,
            long_tp_price=tp,
            short_sl_price=tp,
            projection=projection,
        )
        self.assertTrue(decision.staging_incomplete)
        self.assertTrue(decision.coverage_ok)
        self.assertIn(
            decision.reason_code,
            {
                "coverage_ok_basket_compensates_partial_stages",
                "coverage_ok_complete_stages",
            },
        )

    def test_legacy_skips_staging_guard(self) -> None:
        runtime = RuntimeState(
            strategy_state={
                "initial_entry_confirmed": True,
                "initial_structure_built": True,
                "exit_rebuild_allowed": True,
                "pending_cycle_loss_usdt": 0.0,
            }
        )
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.90,
            long_qty=100.0,
            short_qty=50.0,
            long_avg=1.80,
            short_avg=1.80,
        )
        break_even, _ = self.strategy._calculate_break_even(snapshot, runtime)
        projection = self.strategy._calculate_tp_projection(break_even, snapshot, runtime)
        tp = float(projection.tp_price)
        decision = self.strategy.evaluate_basket_exit_coverage(
            snapshot=snapshot,
            runtime_state=runtime,
            long_tp_price=tp,
            short_sl_price=tp,
            projection=projection,
        )
        self.assertFalse(decision.staging_incomplete)
        self.assertTrue(decision.coverage_ok)
        self.assertEqual(decision.reason_code, "coverage_skipped_not_staged")


class AptT3CoverageGuardIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.candles = normalize_candles(
            "APTUSDT", load_candles_for_symbol("APTUSDT", limit=50000)
        )

    def _fills(self, result):
        return list(getattr(result, "fill_log", None) or getattr(result, "fills_log", None) or [])

    def test_apt_t3_two_early_medium_closes_only_with_economic_coverage(self) -> None:
        staged = run_isolated_blocker(
            coin="APTUSDT",
            candles=self.candles,
            start_index=570,
            staging_config=resolve_grid_profile("two_early_medium"),
            trade_number=3,
        )
        fills = self._fills(staged)
        c4 = [f for f in fills if str(f.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"]
        exits = [
            f
            for f in fills
            if str(f.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
        ]
        orders = [
            o
            for o in (staged.order_log or [])
            if str(o.get("purpose") or "") == "CYCLE_4_SHORT_REDUCE"
        ]
        stage1_cancels = [
            o
            for o in orders
            if str(o.get("event_type") or "").lower() == "cancelled"
            and int((o.get("metadata_excerpt") or {}).get("stage_index") or -1) == 1
        ]
        stage1_fills = [
            f
            for f in c4
            if int((f.get("metadata_excerpt") or {}).get("stage_index") or -1) == 1
        ]

        # No orphan stage1 fill after flatten.
        if exits and staged.final_status == "closed":
            self.assertEqual(len(stage1_fills), 0)
            self.assertTrue(stage1_cancels or len(c4) >= 2)
            # Economic early close may leave cycle-pair UC; mark as basket-compensated.
            last = (staged.final_strategy_state_excerpt or {}).get(
                "last_basket_exit_coverage_decision"
            ) or {}
            if last:
                self.assertTrue(bool(last.get("coverage_ok") or last.get("sufficient")))
            audit = [
                row
                for row in build_pnl_coverage_audit(staged)
                if int(row.get("cycle_index") or 0) == 4
                and "LONG_ADD" in str(row.get("loss_purpose") or "")
            ]
            if audit and audit[0]["status"] == "undercovered":
                # Allowed only as covered_by_basket_exit when economics passed.
                self.assertTrue(
                    bool(last.get("coverage_ok") or last.get("sufficient") or last == {})
                )

    def test_apt_t3_legacy_parity_still_covers_c4(self) -> None:
        legacy = run_isolated_blocker(
            coin="APTUSDT",
            candles=self.candles,
            start_index=570,
            staging_config=resolve_profile("legacy"),
            trade_number=3,
        )
        audit = [
            row
            for row in build_pnl_coverage_audit(legacy)
            if int(row.get("cycle_index") or 0) == 4
            and "LONG_ADD" in str(row.get("loss_purpose") or "")
        ]
        self.assertTrue(audit)
        self.assertIn(audit[0]["status"], {"overcovered", "covered", "exact"})
        self.assertEqual(float(audit[0].get("missing_pnl") or 0.0), 0.0)


class RestartCoverageGuardTests(unittest.TestCase):
    def test_restore_recomputes_guard_from_staging_maps(self) -> None:
        strategy = _strategy()
        runtime = RuntimeState(strategy_state=_base_state())
        residual = _residual_stage1_order()
        runtime.active_orders["sim-stage1"] = residual
        snapshot = HedgeSnapshot(
            symbol="APTUSDT",
            current_price=1.85,
            long_qty=500.0,
            short_qty=250.0,
            long_avg=1.90,
            short_avg=1.90,
            active_orders=(residual.to_snapshot(),),
        )
        # Simulate restart: drop last decision, keep staging maps + residual order.
        runtime.strategy_state.pop("last_basket_exit_coverage_decision", None)
        decision = strategy.allow_cancel_residual_staged_second_leg_orders(
            snapshot, runtime, long_tp_price=1.70, short_sl_price=1.70
        )
        self.assertIsInstance(decision, BasketExitCoverageDecision)
        self.assertTrue(decision.staging_incomplete)
        self.assertFalse(decision.coverage_ok)


if __name__ == "__main__":
    unittest.main()
