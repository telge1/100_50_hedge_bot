"""Tests for backtest-only cycle short-TP distance relief."""

from __future__ import annotations

import json
import unittest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent, snapshot_from_mapping

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
    def test_relief_applies_from_start_cycle_index(self) -> None:
        config = default_cycle_short_tp_relief_config()
        self.assertFalse(relief_applies(config, 1))
        self.assertTrue(relief_applies(config, 2))

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
    def test_aptusdt_c2_example(self) -> None:
        computation = compute_short_tp_relief(
            cycle_index=2,
            long_fill_price=1.9487,
            normal_short_reduce_price=1.9092,
            short_avg_price=1.983,
            short_reduce_qty=4.738,
            required_profit=0.3393065,
            max_distance_pct_from_long_fill=1.0,
        )
        self.assertTrue(computation.cap_applied)
        self.assertAlmostEqual(computation.capped_short_reduce_price, 1.929213, places=4)
        self.assertAlmostEqual(computation.uncovered_loss, 0.0845, places=3)
        self.assertGreater(computation.covered_profit, 0.0)
        self.assertLess(computation.covered_profit, computation.required_profit)


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
        disabled_config = CycleShortTpReliefConfig(enabled=False, start_cycle_index=2)
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
        return CycleShortTpReliefConfig(
            enabled=True,
            start_cycle_index=2,
            max_short_reduce_distance_pct_from_long_fill=1.0,
            carry_uncovered_loss_to_exit=True,
            name="idempotency_test",
        )

    def _build_sim(self) -> HedgeBotOriginalSimulator:
        config_load = resolve_backtest_config(
            config_source="live",
            signal="long",
            symbol="APTUSDT",
        )
        return HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=1.9487,
            config_load=config_load,
            cycle_short_tp_relief_config=self._relief_config(),
        )

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
                current_price=1.94,
                positions={
                    "long_qty": 50.0,
                    "short_qty": 25.0,
                    "long_avg": 1.95,
                    "short_avg": 1.983,
                },
                runtime_state=runtime_state,
                source="test",
            )
            intent = self._short_reduce_intent(
                cycle_index=2,
                normal_price=1.9092,
                qty=4.738,
                long_fill=1.9487,
                short_avg=1.983,
                required_net=0.3393065,
            )
            config = self._relief_config()

            first = _apply_relief_to_short_reduce_intents(
                sim.strategy,
                snapshot,
                runtime_state,
                [intent],
                config=config,
            )
            carry_after_first = get_cumulative_carry_loss(state, trade_block_id="tb-idempotent")
            self.assertAlmostEqual(carry_after_first, 0.084463694, places=5)

            second_intent = self._short_reduce_intent(
                cycle_index=2,
                normal_price=1.9092,
                qty=4.738,
                long_fill=1.9487,
                short_avg=1.983,
                required_net=0.3393065,
            )
            second = _apply_relief_to_short_reduce_intents(
                sim.strategy,
                snapshot,
                runtime_state,
                [second_intent],
                config=config,
            )
            carry_after_second = get_cumulative_carry_loss(state, trade_block_id="tb-idempotent")
            self.assertAlmostEqual(carry_after_second, carry_after_first, places=8)
            self.assertTrue((second[0].metadata or {}).get("short_tp_relief_carry_already_applied"))
            self.assertAlmostEqual(
                float((second[0].metadata or {}).get("uncovered_loss") or 0.0),
                0.084463694,
                places=5,
            )
            self.assertAlmostEqual(
                float((second[0].metadata or {}).get("cumulative_carry_loss") or 0.0),
                carry_after_first,
                places=8,
            )
            self.assertAlmostEqual(float(first[0].trigger_price or 0.0), 1.9292, places=4)
            self.assertAlmostEqual(float(second[0].trigger_price or 0.0), 1.9292, places=4)
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
                current_price=1.88,
                positions={
                    "long_qty": 50.0,
                    "short_qty": 25.0,
                    "long_avg": 1.90,
                    "short_avg": 1.92,
                },
                runtime_state=runtime_state,
                source="test",
            )
            config = self._relief_config()
            c2 = self._short_reduce_intent(
                cycle_index=2,
                normal_price=1.9092,
                qty=4.738,
                long_fill=1.9487,
                short_avg=1.983,
                required_net=0.3393065,
            )
            c3 = self._short_reduce_intent(
                cycle_index=3,
                normal_price=1.843,
                qty=4.7,
                long_fill=1.8858,
                short_avg=1.92,
                required_net=0.35,
            )

            _apply_relief_to_short_reduce_intents(
                sim.strategy, snapshot, runtime_state, [c2], config=config
            )
            after_c2 = get_cumulative_carry_loss(state, trade_block_id="tb-multi-cycle")
            _apply_relief_to_short_reduce_intents(
                sim.strategy, snapshot, runtime_state, [c3], config=config
            )
            after_c3 = get_cumulative_carry_loss(state, trade_block_id="tb-multi-cycle")

            self.assertAlmostEqual(after_c2, 0.084463694, places=5)
            self.assertGreater(after_c3, after_c2)
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
            cycle_index=2,
            purpose="CYCLE_2_SHORT_REDUCE",
            capped_short_reduce_price=1.9292,
            short_reduce_qty=4.738,
        )
        first_total, first_new = _register_carry_loss(
            state,
            trade_block_id="tb-key-test",
            applied_key=key,
            uncovered_loss=0.084463694,
            record={"cycle_index": 2},
        )
        second_total, second_new = _register_carry_loss(
            state,
            trade_block_id="tb-key-test",
            applied_key=key,
            uncovered_loss=0.084463694,
            record={"cycle_index": 2},
        )
        self.assertTrue(first_new)
        self.assertFalse(second_new)
        self.assertAlmostEqual(first_total, second_total, places=8)
        self.assertAlmostEqual(first_total, 0.084463694, places=5)


@unittest.skipUnless(
    (DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")).exists(),
    "APTUSDT candle data unavailable",
)
class CycleShortTpReliefAptIntegrationTests(unittest.TestCase):
    def _stuck_trade_window(self) -> list[dict]:
        candles = load_candles_for_symbol("APTUSDT", limit=50000)
        return candles[250 : 250 + 1000]

    def test_c2_short_reduce_is_capped_with_export_fields(self) -> None:
        relief = CycleShortTpReliefConfig(
            enabled=True,
            start_cycle_index=2,
            max_short_reduce_distance_pct_from_long_fill=1.0,
            carry_uncovered_loss_to_exit=True,
            name="test",
        )
        result = run_historical_backtest(
            "APTUSDT",
            "long",
            self._stuck_trade_window(),
            max_candles=999,
            config_source="live",
            cycle_short_tp_relief_config=relief,
        )
        c2_short = None
        for record in result.intent_log:
            purpose = str(record.get("purpose") or "")
            if "CYCLE_2_SHORT_REDUCE" not in purpose:
                continue
            c2_short = record
            break
        self.assertIsNotNone(c2_short)
        assert c2_short is not None
        excerpt = c2_short.get("metadata_excerpt") or {}
        self.assertAlmostEqual(float(c2_short.get("trigger_price") or 0.0), 1.9292, places=4)
        self.assertAlmostEqual(float(excerpt.get("normal_short_reduce_price") or 0.0), 1.9092, places=4)
        self.assertAlmostEqual(float(excerpt.get("first_leg_fill_price") or 0.0), 1.9487, places=4)
        self.assertAlmostEqual(float(excerpt.get("uncovered_loss") or 0.0), 0.0845, places=3)
        self.assertAlmostEqual(float(excerpt.get("cumulative_carry_loss") or 0.0), 0.0845, places=3)

        rows = build_trade_block_rows(result)
        short_rows = [
            row
            for row in rows
            if row.get("row_type") == "intent"
            and "CYCLE_2_SHORT_REDUCE" in str(row.get("purpose") or "")
        ]
        short_row = short_rows[0]
        for field in (
            "normal_short_reduce_price",
            "capped_short_reduce_price",
            "required_profit",
            "covered_profit",
            "uncovered_loss",
            "cumulative_carry_loss",
            "max_short_reduce_distance_pct_from_long_fill",
        ):
            self.assertIsNotNone(short_row.get(field), msg=f"missing export field {field}")

        exit_rows = [
            row
            for row in rows
            if row.get("row_type") == "intent"
            and str(row.get("purpose") or "") in {"LONG_TP_EXIT", "SHORT_SL_EXIT"}
            and row.get("cumulative_carry_loss")
        ]
        self.assertTrue(exit_rows)
        self.assertIsNotNone(exit_rows[0].get("exit_adjustment_pct"))

    def test_start_250_no_duplicate_carry_loss_accumulation(self) -> None:
        relief = CycleShortTpReliefConfig(
            enabled=True,
            start_cycle_index=2,
            max_short_reduce_distance_pct_from_long_fill=1.0,
            carry_uncovered_loss_to_exit=True,
            name="start_250_no_double_count",
        )
        result = run_historical_backtest(
            "APTUSDT",
            "long",
            self._stuck_trade_window(),
            max_candles=999,
            config_source="live",
            cycle_short_tp_relief_config=relief,
        )
        self.assertEqual(result.final_status, "closed")

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

        self.assertTrue(relief_intents)
        expected_by_key: dict[tuple[str, float, float, float], float] = {}
        for item in relief_intents:
            key = (item["purpose"], item["normal"], item["capped"], item["qty"])
            expected_by_key.setdefault(key, item["uncovered"])
        expected_total = sum(expected_by_key.values())
        final_cumulative = max(item["cumulative"] for item in relief_intents)

        self.assertAlmostEqual(final_cumulative, expected_total, places=5)
        self.assertLessEqual(final_cumulative, expected_total + 1e-9)

        from collections import Counter

        duplicate_signatures = Counter(
            (item["purpose"], item["normal"], item["capped"], item["qty"]) for item in relief_intents
        )
        for signature, count in duplicate_signatures.items():
            if count <= 1:
                continue
            matching = [item for item in relief_intents if (item["purpose"], item["normal"], item["capped"], item["qty"]) == signature]
            cumulative_values = [item["cumulative"] for item in matching]
            self.assertEqual(
                len(set(cumulative_values)),
                1,
                msg=f"duplicate relief signature {signature} changed cumulative_carry_loss: {matching}",
            )

    def test_carry_loss_preserved_after_refill_avg_size_change(self) -> None:
        config_load = resolve_backtest_config(
            config_source="live",
            signal="long",
            symbol="APTUSDT",
        )
        relief = default_cycle_short_tp_relief_config()
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=1.9534,
            config_load=config_load,
            cycle_short_tp_relief_config=relief,
        )
        try:
            runtime_state = sim.runtime_state
            state = runtime_state.strategy_state
            state["trade_block_id"] = "tb-relief-refill-test"
            carry_loss = 0.084463694
            _add_carry_loss(
                state,
                trade_block_id="tb-relief-refill-test",
                uncovered_loss=carry_loss,
                record={"cycle_index": 2, "uncovered_loss": carry_loss},
            )

            before_snapshot = snapshot_from_mapping(
                symbol="APTUSDT",
                current_price=1.9534,
                positions={
                    "long_qty": 50.427,
                    "short_qty": 25.214,
                    "long_avg": 1.95338234,
                    "short_avg": 1.95347019,
                },
                runtime_state=runtime_state,
                source="before_refill",
            )
            after_snapshot = snapshot_from_mapping(
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
            runtime_state.last_snapshot = after_snapshot
            state["pending_cycle_loss_usdt"] = 0.0

            break_even, _ = sim.strategy._calculate_break_even(after_snapshot, runtime_state)
            before_projection = sim.strategy._calculate_tp_projection(
                break_even,
                before_snapshot,
                runtime_state,
            )
            after_projection = sim.strategy._calculate_tp_projection(
                break_even,
                after_snapshot,
                runtime_state,
            )

            self.assertAlmostEqual(
                get_cumulative_carry_loss(state, trade_block_id="tb-relief-refill-test"),
                carry_loss,
                places=6,
            )
            self.assertNotAlmostEqual(before_projection.tp_price, after_projection.tp_price, places=4)
            self.assertGreaterEqual(before_projection.required_profit_to_cover_loss, carry_loss)
            self.assertGreaterEqual(after_projection.required_profit_to_cover_loss, carry_loss)
            relief_meta = state.get("_last_exit_short_tp_relief") or {}
            self.assertAlmostEqual(float(relief_meta.get("cumulative_carry_loss") or 0.0), carry_loss, places=6)
            self.assertIsNotNone(relief_meta.get("exit_adjustment_pct"))
        finally:
            sim.close()


if __name__ == "__main__":
    unittest.main()
