"""Tests for backtest-only cycle short-TP distance relief."""

from __future__ import annotations

import json
import re
import unittest
from collections import Counter

from fixed_cycle_hedge_bot.models import StrategyIntent, snapshot_from_mapping

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol, symbol_to_feather_name
from research.backtests.cycle_short_tp_relief import (
    CycleShortTpReliefConfig,
    compute_short_tp_relief,
    config_from_json_string,
    default_cycle_short_tp_relief_config,
    relief_applies,
)
from research.backtests.cycle_short_tp_relief_shim import (
    _add_carry_loss,
    _apply_relief_to_short_reduce_intents,
    _build_relief_applied_key,
    _register_carry_loss,
    get_cumulative_carry_loss,
)
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.simulated_order_book import SyntheticCandle
from research.backtests.trade_block_export import build_trade_block_rows


class CycleShortTpReliefConfigTests(unittest.TestCase):
    def test_default_config_starts_at_cycle_4_with_four_pct(self) -> None:
        config = default_cycle_short_tp_relief_config()
        self.assertTrue(config.enabled)
        self.assertEqual(config.start_cycle_index, 4)
        self.assertAlmostEqual(config.max_short_reduce_distance_pct_from_long_fill, 4.0)
        self.assertTrue(config.carry_uncovered_loss_to_exit)

    def test_relief_applies_from_start_cycle_index(self) -> None:
        config = default_cycle_short_tp_relief_config()
        for cycle_index in (1, 2, 3):
            self.assertFalse(relief_applies(config, cycle_index), msg=f"cycle {cycle_index}")
        for cycle_index in (4, 5, 6):
            self.assertTrue(relief_applies(config, cycle_index), msg=f"cycle {cycle_index}")

    def test_config_from_json_string(self) -> None:
        payload = {
            "enabled": True,
            "start_cycle_index": 3,
            "max_short_reduce_distance_pct_from_long_fill": 0.75,
            "carry_uncovered_loss_to_exit": False,
            "bands": [
                {
                    "min_cycle_index": 3,
                    "max_cycle_index": 5,
                    "max_short_reduce_distance_pct_from_long_fill": 0.5,
                }
            ],
        }
        config = config_from_json_string(json.dumps(payload))
        self.assertEqual(config.start_cycle_index, 3)
        self.assertFalse(config.carry_uncovered_loss_to_exit)
        self.assertAlmostEqual(
            config.bands[0].max_short_reduce_distance_pct_from_long_fill,
            0.5,
        )


class CycleShortTpReliefComputationTests(unittest.TestCase):
    def test_c4_cap_and_uncovered_loss_formula(self) -> None:
        long_fill = 1.8323
        normal = 1.7061
        qty = 4.738
        capped = long_fill * 0.96
        computation = compute_short_tp_relief(
            cycle_index=4,
            long_fill_price=long_fill,
            normal_short_reduce_price=normal,
            short_avg_price=1.818,
            short_reduce_qty=qty,
            required_profit=0.5,
            max_distance_pct_from_long_fill=4.0,
        )
        self.assertTrue(computation.cap_applied)
        self.assertAlmostEqual(computation.capped_short_reduce_price, capped, places=6)
        expected_uncovered = qty * (capped - normal)
        self.assertAlmostEqual(computation.uncovered_loss, expected_uncovered, places=6)

    def test_no_cap_when_normal_is_above_floor(self) -> None:
        computation = compute_short_tp_relief(
            cycle_index=4,
            long_fill_price=1.8323,
            normal_short_reduce_price=1.80,
            short_avg_price=1.818,
            short_reduce_qty=4.738,
            required_profit=0.5,
            max_distance_pct_from_long_fill=4.0,
        )
        self.assertFalse(computation.cap_applied)
        self.assertAlmostEqual(computation.uncovered_loss, 0.0)


class CycleShortTpReliefShimTests(unittest.TestCase):
    def test_disabled_config_does_not_patch_strategy(self) -> None:
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=10.0,
            cycle_short_tp_relief_config=None,
        )
        self.assertFalse(
            getattr(sim.strategy, "_backtest_cycle_short_tp_relief_installed", False)
        )
        sim.close()

    def test_enabled_config_patches_strategy(self) -> None:
        config = default_cycle_short_tp_relief_config()
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=10.0,
            cycle_short_tp_relief_config=config,
        )
        self.assertTrue(
            getattr(sim.strategy, "_backtest_cycle_short_tp_relief_installed", False)
        )
        sim.close()

    def test_disabled_config_object_is_noop(self) -> None:
        config = default_cycle_short_tp_relief_config()
        config.enabled = False
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=10.0,
            cycle_short_tp_relief_config=config,
        )
        self.assertFalse(
            getattr(sim.strategy, "_backtest_cycle_short_tp_relief_installed", False)
        )
        sim.close()


class CycleShortTpReliefBaselineIdentityTests(unittest.TestCase):
    def _sample_candles(self) -> list[SyntheticCandle]:
        prices = [10.0, 10.1, 9.9, 10.05, 9.95, 10.2, 10.0, 9.8, 10.1, 10.3]
        return [
            SyntheticCandle(symbol="APTUSDT", close=price, open=price, high=price, low=price)
            for price in prices
        ]

    def test_enabled_false_matches_baseline(self) -> None:
        candles = self._sample_candles()
        baseline = run_historical_backtest(
            "APTUSDT",
            "long",
            candles,
            max_candles=len(candles) - 1,
            config_source="test",
        )
        disabled_config = CycleShortTpReliefConfig(enabled=False, start_cycle_index=4)
        disabled = run_historical_backtest(
            "APTUSDT",
            "long",
            candles,
            max_candles=len(candles) - 1,
            config_source="test",
            cycle_short_tp_relief_config=disabled_config,
        )
        self.assertEqual(baseline.final_status, disabled.final_status)
        self.assertEqual(baseline.realized_pnl, disabled.realized_pnl)
        self.assertEqual(baseline.fills_count, disabled.fills_count)
        self.assertEqual(baseline.orders_submitted, disabled.orders_submitted)


class CycleShortTpReliefIdempotencyTests(unittest.TestCase):
    def _relief_config(self) -> CycleShortTpReliefConfig:
        return default_cycle_short_tp_relief_config()

    def _build_sim(self) -> HedgeBotOriginalSimulator:
        config_load = resolve_backtest_config(
            config_source="live",
            signal="long",
            symbol="APTUSDT",
        )
        return HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=1.8323,
            config_load=config_load,
            cycle_short_tp_relief_config=self._relief_config(),
        )


@unittest.skipUnless(
    (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    "APTUSDT candle data unavailable",
)
class LiveShortTpReliefBacktestPathTests(unittest.TestCase):
    def test_live_relief_path_uses_live_strategy_without_shim(self) -> None:
        """Backtest-Pfad nutzt bei use_live_short_tp_relief ausschließlich den Live-Strategiepfad."""
        from fixed_cycle_hedge_bot.fixed_cycle_strategy import FixedCycleHedgeStrategy

        calls: list[dict] = []
        original_apply = FixedCycleHedgeStrategy._apply_live_short_tp_relief

        def wrapped_apply(self, snapshot, runtime_state, intents):
            calls.append(
                {
                    "snapshot_price": float(getattr(snapshot, "current_price", 0.0) or 0.0),
                    "intent_count": len(intents or []),
                }
            )
            return original_apply(self, snapshot, runtime_state, intents)

        FixedCycleHedgeStrategy._apply_live_short_tp_relief = wrapped_apply
        try:
            # Kleines Fenster aus APTUSDT-Candles; run_historical_backtest normalisiert selbst.
            candles = load_candles_for_symbol("APTUSDT", limit=900)[800:860]
            baseline = run_historical_backtest(
                "APTUSDT",
                "long",
                candles,
                max_candles=59,
                config_source="live",
            )
            live_relief = run_historical_backtest(
                "APTUSDT",
                "long",
                candles,
                max_candles=59,
                config_source="live",
                cycle_short_tp_relief_config=None,
                use_live_short_tp_relief=True,
            )
            # Backtests sollten regulär laufen.
            self.assertIn(baseline.final_status, {"closed", "open", "max_candles"})
            self.assertIn(live_relief.final_status, {"closed", "open", "max_candles"})
            # Shim-spezifischer State-Schlüssel darf im Live-Relief-Pfad nicht existieren.
            self.assertNotIn("_backtest_cycle_short_tp_relief", dict(live_relief.strategy_state or {}))
            # Live-Hook muss mindestens einmal aufgerufen worden sein.
            self.assertTrue(calls, "expected _apply_live_short_tp_relief to be called in backtest path")
        finally:
            FixedCycleHedgeStrategy._apply_live_short_tp_relief = original_apply

    def _short_reduce_intent(
        self,
        *,
        cycle_index: int,
        normal_price: float,
        qty: float,
        long_fill: float,
        short_avg: float,
        required_net: float,
    ) -> StrategyIntent:
        purpose = f"CYCLE_{cycle_index}_SHORT_REDUCE"
        return StrategyIntent(
            side="short",
            qty=qty,
            purpose=purpose,
            order_type="Limit",
            trigger_price=normal_price,
            metadata={
                "cycle_index": cycle_index,
                "first_leg_fill_price": long_fill,
                "short_entry_price": short_avg,
                "required_net": required_net,
            },
        )

    def test_same_short_reduce_intent_twice_adds_carry_loss_once(self) -> None:
        sim = self._build_sim()
        try:
            runtime_state = sim.runtime_state
            state = runtime_state.strategy_state
            state["trade_block_id"] = "tb-idempotent"
            snapshot = snapshot_from_mapping(
                symbol="APTUSDT",
                current_price=1.75,
                positions={
                    "long_qty": 50.0,
                    "short_qty": 25.0,
                    "long_avg": 1.82,
                    "short_avg": 1.818,
                },
                runtime_state=runtime_state,
                source="test",
            )
            qty = 4.738
            normal = 1.7061
            long_fill = 1.8323
            expected_uncovered = qty * (long_fill * 0.96 - normal)
            config = self._relief_config()

            first = _apply_relief_to_short_reduce_intents(
                sim.strategy,
                snapshot,
                runtime_state,
                [
                    self._short_reduce_intent(
                        cycle_index=4,
                        normal_price=normal,
                        qty=qty,
                        long_fill=long_fill,
                        short_avg=1.818,
                        required_net=0.5,
                    )
                ],
                config=config,
            )
            carry_after_first = get_cumulative_carry_loss(state, trade_block_id="tb-idempotent")
            self.assertAlmostEqual(carry_after_first, expected_uncovered, places=5)

            second = _apply_relief_to_short_reduce_intents(
                sim.strategy,
                snapshot,
                runtime_state,
                [
                    self._short_reduce_intent(
                        cycle_index=4,
                        normal_price=normal,
                        qty=qty,
                        long_fill=long_fill,
                        short_avg=1.818,
                        required_net=0.5,
                    )
                ],
                config=config,
            )
            carry_after_second = get_cumulative_carry_loss(state, trade_block_id="tb-idempotent")
            self.assertAlmostEqual(carry_after_second, carry_after_first, places=8)
            self.assertTrue((second[0].metadata or {}).get("short_tp_relief_carry_already_applied"))
            self.assertAlmostEqual(float(first[0].trigger_price or 0.0), long_fill * 0.96, places=3)
            self.assertAlmostEqual(float(second[0].trigger_price or 0.0), long_fill * 0.96, places=3)
        finally:
            sim.close()

    def test_different_cycle_short_reduce_orders_each_add_once(self) -> None:
        sim = self._build_sim()
        try:
            runtime_state = sim.runtime_state
            state = runtime_state.strategy_state
            state["trade_block_id"] = "tb-multi-cycle"
            snapshot = snapshot_from_mapping(
                symbol="APTUSDT",
                current_price=1.75,
                positions={
                    "long_qty": 50.0,
                    "short_qty": 25.0,
                    "long_avg": 1.82,
                    "short_avg": 1.818,
                },
                runtime_state=runtime_state,
                source="test",
            )
            config = self._relief_config()
            c4 = self._short_reduce_intent(
                cycle_index=4,
                normal_price=1.7061,
                qty=4.738,
                long_fill=1.8323,
                short_avg=1.818,
                required_net=0.5,
            )
            c5 = self._short_reduce_intent(
                cycle_index=5,
                normal_price=1.5727,
                qty=4.7,
                long_fill=1.6976,
                short_avg=1.818,
                required_net=0.55,
            )

            _apply_relief_to_short_reduce_intents(
                sim.strategy, snapshot, runtime_state, [c4], config=config
            )
            after_c4 = get_cumulative_carry_loss(state, trade_block_id="tb-multi-cycle")
            _apply_relief_to_short_reduce_intents(
                sim.strategy, snapshot, runtime_state, [c5], config=config
            )
            after_c5 = get_cumulative_carry_loss(state, trade_block_id="tb-multi-cycle")

            self.assertGreater(after_c4, 0.0)
            self.assertGreater(after_c5, after_c4)
            self.assertEqual(
                len(
                    (state.get("_backtest_cycle_short_tp_relief") or {})
                    .get("applied_relief_keys_by_trade_block", {})
                    .get("tb-multi-cycle", {})
                ),
                2,
            )
        finally:
            sim.close()

    def test_register_carry_loss_is_idempotent_for_same_key(self) -> None:
        state: dict[str, object] = {"trade_block_id": "tb-key-test"}
        key = _build_relief_applied_key(
            trade_block_id="tb-key-test",
            cycle_index=4,
            purpose="CYCLE_4_SHORT_REDUCE",
            capped_short_reduce_price=1.759,
            short_reduce_qty=4.738,
        )
        first_total, first_new = _register_carry_loss(
            state,
            trade_block_id="tb-key-test",
            applied_key=key,
            uncovered_loss=0.25,
            record={"cycle_index": 4},
        )
        second_total, second_new = _register_carry_loss(
            state,
            trade_block_id="tb-key-test",
            applied_key=key,
            uncovered_loss=0.25,
            record={"cycle_index": 4},
        )
        self.assertTrue(first_new)
        self.assertFalse(second_new)
        self.assertAlmostEqual(first_total, second_total, places=8)
        self.assertAlmostEqual(first_total, 0.25, places=5)

    def test_cycles_below_start_are_unchanged(self) -> None:
        sim = self._build_sim()
        try:
            runtime_state = sim.runtime_state
            state = runtime_state.strategy_state
            state["trade_block_id"] = "tb-early-cycles"
            snapshot = snapshot_from_mapping(
                symbol="APTUSDT",
                current_price=1.95,
                positions={
                    "long_qty": 50.0,
                    "short_qty": 25.0,
                    "long_avg": 1.98,
                    "short_avg": 1.983,
                },
                runtime_state=runtime_state,
                source="test",
            )
            config = self._relief_config()
            original = self._short_reduce_intent(
                cycle_index=2,
                normal_price=1.9092,
                qty=4.738,
                long_fill=1.9487,
                short_avg=1.983,
                required_net=0.3393065,
            )
            unchanged = _apply_relief_to_short_reduce_intents(
                sim.strategy,
                snapshot,
                runtime_state,
                [original],
                config=config,
            )
            self.assertAlmostEqual(float(unchanged[0].trigger_price or 0.0), 1.9092, places=4)
            self.assertFalse((unchanged[0].metadata or {}).get("short_tp_relief_cap_applied"))
            self.assertAlmostEqual(
                get_cumulative_carry_loss(state, trade_block_id="tb-early-cycles"),
                0.0,
            )
        finally:
            sim.close()


@unittest.skipUnless(
    (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    "APTUSDT candle data unavailable",
)
class CycleShortTpReliefAptIntegrationTests(unittest.TestCase):
    def _stuck_trade_window(self) -> list[dict]:
        candles = load_candles_for_symbol("APTUSDT", limit=50000)
        return candles[250 : 250 + 15000]

    def _run_with_default_relief(self, *, max_candles: int) -> tuple:
        baseline = run_historical_backtest(
            "APTUSDT",
            "long",
            self._stuck_trade_window(),
            max_candles=max_candles,
            config_source="live",
        )
        relief = run_historical_backtest(
            "APTUSDT",
            "long",
            self._stuck_trade_window(),
            max_candles=max_candles,
            config_source="live",
            cycle_short_tp_relief_config=default_cycle_short_tp_relief_config(),
        )
        return baseline, relief

    def _short_reduce_intents(self, result) -> dict[int, list[dict]]:
        by_cycle: dict[int, list[dict]] = {}
        for record in result.intent_log:
            purpose = str(record.get("purpose") or "")
            if "SHORT_REDUCE" not in purpose:
                continue
            excerpt = record.get("metadata_excerpt") or {}
            cycle_index = int(record.get("cycle_index") or excerpt.get("cycle_index") or 0)
            if cycle_index <= 0:
                match = re.search(r"CYCLE_(\d+)_SHORT_REDUCE", purpose, re.I)
                if match:
                    cycle_index = int(match.group(1))
            by_cycle.setdefault(cycle_index, []).append(record)
        return by_cycle

    def _first_short_reduce_trigger(self, records: list[dict]) -> float:
        for record in records:
            trigger = record.get("trigger_price")
            if trigger is not None:
                return float(trigger)
            excerpt = record.get("metadata_excerpt") or {}
            if excerpt.get("trigger_price") is not None:
                return float(excerpt.get("trigger_price"))
        return 0.0

    def test_c1_to_c3_unchanged_with_default_relief(self) -> None:
        baseline, relief = self._run_with_default_relief(max_candles=14999)
        baseline_short = self._short_reduce_intents(baseline)
        relief_short = self._short_reduce_intents(relief)
        for cycle_index in (1, 2, 3):
            self.assertIn(cycle_index, baseline_short)
            self.assertIn(cycle_index, relief_short)
            base_trigger = self._first_short_reduce_trigger(baseline_short[cycle_index])
            relief_trigger = self._first_short_reduce_trigger(relief_short[cycle_index])
            self.assertAlmostEqual(base_trigger, relief_trigger, places=4, msg=f"cycle {cycle_index}")

    def test_c4_short_reduce_capped_at_four_pct_with_export_fields(self) -> None:
        _, relief = self._run_with_default_relief(max_candles=14999)
        c4_records = [
            record
            for record in relief.intent_log
            if "CYCLE_4_SHORT_REDUCE" in str(record.get("purpose") or "")
        ]
        self.assertTrue(c4_records)
        c4 = c4_records[0]
        excerpt = c4.get("metadata_excerpt") or {}
        long_fill = float(excerpt.get("first_leg_fill_price") or 0.0)
        normal = float(excerpt.get("normal_short_reduce_price") or 0.0)
        capped = float(excerpt.get("capped_short_reduce_price") or 0.0)
        trigger = float(c4.get("trigger_price") or 0.0)
        self.assertGreater(long_fill, 0.0)
        self.assertAlmostEqual(capped, long_fill * 0.96, places=4)
        if normal + 1e-9 < capped:
            self.assertTrue(excerpt.get("short_tp_relief_cap_applied"))
            self.assertAlmostEqual(trigger, capped, places=3)
            qty = float(c4.get("qty") or 0.0)
            self.assertAlmostEqual(
                float(excerpt.get("uncovered_loss") or 0.0),
                qty * (capped - normal),
                places=4,
            )

        rows = build_trade_block_rows(relief)
        short_rows = [
            row
            for row in rows
            if row.get("row_type") == "intent"
            and "CYCLE_4_SHORT_REDUCE" in str(row.get("purpose") or "")
        ]
        self.assertTrue(short_rows)
        short_row = short_rows[0]
        for field in (
            "normal_short_reduce_price",
            "capped_short_reduce_price",
            "short_tp_relief_cap_applied",
            "uncovered_loss",
            "cumulative_carry_loss",
        ):
            self.assertIsNotNone(short_row.get(field), msg=f"missing export field {field}")

        exit_rows = [
            row
            for row in rows
            if row.get("row_type") == "intent"
            and str(row.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
            and row.get("cumulative_carry_loss")
        ]
        if exit_rows:
            self.assertIsNotNone(exit_rows[0].get("exit_adjustment_pct"))

    def test_no_duplicate_carry_loss_accumulation(self) -> None:
        _, result = self._run_with_default_relief(max_candles=14999)
        relief_intents = []
        for record in result.intent_log:
            excerpt = record.get("metadata_excerpt") or {}
            uncovered = float(excerpt.get("uncovered_loss") or 0.0)
            if uncovered <= 0 or "SHORT_REDUCE" not in str(record.get("purpose") or ""):
                continue
            relief_intents.append(
                {
                    "purpose": str(record.get("purpose") or ""),
                    "normal": round(float(excerpt.get("normal_short_reduce_price") or 0.0), 6),
                    "capped": round(float(excerpt.get("capped_short_reduce_price") or 0.0), 6),
                    "qty": round(float(record.get("qty") or 0.0), 6),
                    "uncovered": round(uncovered, 6),
                    "cumulative": round(float(excerpt.get("cumulative_carry_loss") or 0.0), 6),
                }
            )
        if not relief_intents:
            self.skipTest("no capped short-reduce intents in this window")

        expected_by_key: dict[tuple[str, float, float, float], float] = {}
        for item in relief_intents:
            key = (item["purpose"], item["normal"], item["capped"], item["qty"])
            expected_by_key.setdefault(key, item["uncovered"])
        expected_total = sum(expected_by_key.values())
        final_cumulative = max(item["cumulative"] for item in relief_intents)
        self.assertAlmostEqual(final_cumulative, expected_total, places=5)

        duplicate_signatures = Counter(
            (item["purpose"], item["normal"], item["capped"], item["qty"]) for item in relief_intents
        )
        for signature, count in duplicate_signatures.items():
            if count <= 1:
                continue
            matching = [
                item
                for item in relief_intents
                if (item["purpose"], item["normal"], item["capped"], item["qty"]) == signature
            ]
            cumulative_values = [item["cumulative"] for item in matching]
            self.assertEqual(
                len(set(cumulative_values)),
                1,
                msg=f"duplicate relief signature {signature} changed cumulative_carry_loss: {matching}",
            )

    def test_carry_loss_affects_final_exit_projection(self) -> None:
        config_load = resolve_backtest_config(
            config_source="live",
            signal="long",
            symbol="APTUSDT",
        )
        relief = default_cycle_short_tp_relief_config()
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=1.8183,
            config_load=config_load,
            cycle_short_tp_relief_config=relief,
        )
        try:
            runtime_state = sim.runtime_state
            state = runtime_state.strategy_state
            state["trade_block_id"] = "tb-relief-exit-test"
            carry_loss = 0.25
            _add_carry_loss(
                state,
                trade_block_id="tb-relief-exit-test",
                uncovered_loss=carry_loss,
                record={"cycle_index": 4, "uncovered_loss": carry_loss},
            )

            snapshot = snapshot_from_mapping(
                symbol="APTUSDT",
                current_price=1.8183,
                positions={
                    "long_qty": 50.427,
                    "short_qty": 25.213,
                    "long_avg": 1.81833879,
                    "short_avg": 1.81846513,
                },
                runtime_state=runtime_state,
                source="after_refill",
            )
            runtime_state.last_snapshot = snapshot
            state["pending_cycle_loss_usdt"] = 0.0

            break_even, _ = sim.strategy._calculate_break_even(snapshot, runtime_state)
            with_carry = sim.strategy._calculate_tp_projection(
                break_even,
                snapshot,
                runtime_state,
            )
            relief_meta = dict(state.get("_last_exit_short_tp_relief") or {})

            relief_state = state.get("_backtest_cycle_short_tp_relief") or {}
            relief_state["carry_loss_by_trade_block"]["tb-relief-exit-test"] = 0.0
            baseline_projection = sim.strategy._calculate_tp_projection(
                break_even,
                snapshot,
                runtime_state,
            )

            self.assertGreater(
                with_carry.required_profit_to_cover_loss,
                baseline_projection.required_profit_to_cover_loss,
            )
            self.assertAlmostEqual(with_carry.required_profit_to_cover_loss, carry_loss, places=6)
            self.assertAlmostEqual(float(relief_meta.get("cumulative_carry_loss") or 0.0), carry_loss, places=6)
            self.assertIsNotNone(relief_meta.get("exit_adjustment_pct"))
            self.assertGreater(with_carry.tp_price, baseline_projection.tp_price)
        finally:
            sim.close()


if __name__ == "__main__":
    unittest.main()
