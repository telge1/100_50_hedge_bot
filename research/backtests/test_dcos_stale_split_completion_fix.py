"""Tests for DCOS backtest-only stale split completion shim."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.dynamic_cycle_order_scaling import config_from_json_string
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import resolve_backtest_config
from research.backtests.purpose_utils import preserve_bot_purpose

MILD_A_CONFIG = Path("research/backtests/configs/dcos_mild_qty/variant_a.json")
C5_SR = "CYCLE_5_SHORT_REDUCE"
C6_LA = "CYCLE_6_LONG_ADD"
REPRO_STARTS = [250, 4750]


def _run_to_c5_sr(start_index: int, *, scaling_config_path: Path | None) -> tuple:
    raw = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=50000)
    candles = normalize_candles("APTUSDT", raw)
    window = candles[start_index : start_index + 5000]
    scaling = (
        config_from_json_string(scaling_config_path.read_text())
        if scaling_config_path is not None
        else None
    )
    config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    sim = HedgeBotOriginalSimulator(
        signal="long",
        symbol="APTUSDT",
        candle_close=float(window[0].close),
        config_load=config_load,
        dynamic_cycle_scaling_config=scaling,
    )
    sim.candle = window[0]
    sim.candle_index = start_index
    entry = sim.run_entry_smoke()
    sim.submit_intents_to_book(entry.entry_intents, event_source="initial")

    c5_sr_seen = False
    c6_la_seen = False
    c5_sr_candle: int | None = None

    for offset, candle in enumerate(window[1:], start=1):
        result = sim.process_candle(candle, fill_model="conservative")
        abs_index = start_index + offset
        for fill in result.candle_fills:
            if preserve_bot_purpose(fill.purpose) == C5_SR:
                c5_sr_seen = True
                c5_sr_candle = abs_index
        for intent in result.on_fill_intents + result.tick_intents:
            if preserve_bot_purpose(intent.purpose) == C6_LA:
                c6_la_seen = True
        if c5_sr_seen:
            break

    state = sim.runtime_state.strategy_state
    strategy = sim.strategy
    entry5 = strategy._get_cycle_sequence_entry(sim.runtime_state, 5)
    audit = state.get("_dcos_backtest_audit_events") or []
    fix_events = [
        event for event in audit if event.get("event") == "dcos_stale_split_completion_fix_applied"
    ]
    return {
        "start_index": start_index,
        "c5_sr_seen": c5_sr_seen,
        "c5_sr_candle": c5_sr_candle,
        "c6_la_on_fill_or_tick": c6_la_seen,
        "has_c6_in_intent_log": any(
            preserve_bot_purpose(entry.get("purpose") or "") == C6_LA
            for entry in sim.intent_log
        ),
        "cycle_long_add_filled": bool(state.get("cycle_long_add_filled")),
        "cycle_short_tp_filled": bool(state.get("cycle_short_tp_filled")),
        "c5_complete": bool(entry5.get("complete")),
        "next_required_purpose": state.get("next_required_purpose"),
        "fix_events": fix_events,
        "sim": sim,
    }


class DcosStaleSplitCompletionFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mild_config = config_from_json_string(MILD_A_CONFIG.read_text())

    def test_shim_installs_try_complete_wrapper(self) -> None:
        raw = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=100)
        candles = normalize_candles("APTUSDT", raw)
        config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=float(candles[0].close),
            config_load=config_load,
            dynamic_cycle_scaling_config=self.mild_config,
        )
        self.assertTrue(
            getattr(sim.strategy, "_backtest_dcos_stale_split_completion_shim_installed", False)
        )
        sim.close()

    def test_baseline_without_dcos_has_no_completion_shim(self) -> None:
        raw = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=100)
        candles = normalize_candles("APTUSDT", raw)
        config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
        sim = HedgeBotOriginalSimulator(
            signal="long",
            symbol="APTUSDT",
            candle_close=float(candles[0].close),
            config_load=config_load,
            dynamic_cycle_scaling_config=None,
        )
        self.assertFalse(
            getattr(sim.strategy, "_backtest_dcos_stale_split_completion_shim_installed", False)
        )
        sim.close()

    @unittest.skipUnless(MILD_A_CONFIG.is_file(), "mild A config missing")
    def test_mild_repro_start_250_submits_c6_after_c5_sr(self) -> None:
        outcome = _run_to_c5_sr(250, scaling_config_path=MILD_A_CONFIG)
        self.assertTrue(outcome["c5_sr_seen"], "expected C5-SR fill")
        self.assertTrue(
            outcome["c6_la_on_fill_or_tick"] or outcome["has_c6_in_intent_log"],
            "expected CYCLE_6_LONG_ADD after C5-SR fix",
        )
        self.assertTrue(outcome["fix_events"], "expected dcos_stale_split_completion_fix_applied")
        self.assertTrue(outcome["c5_complete"], "cycle 5 should be complete")
        self.assertFalse(outcome["cycle_long_add_filled"])
        self.assertFalse(outcome["cycle_short_tp_filled"])

    @unittest.skipUnless(MILD_A_CONFIG.is_file(), "mild A config missing")
    def test_mild_repro_start_4750_submits_c6_after_c5_sr(self) -> None:
        outcome = _run_to_c5_sr(4750, scaling_config_path=MILD_A_CONFIG)
        self.assertTrue(outcome["c5_sr_seen"], "expected C5-SR fill")
        self.assertTrue(
            outcome["c6_la_on_fill_or_tick"] or outcome["has_c6_in_intent_log"],
            "expected CYCLE_6_LONG_ADD after C5-SR fix",
        )
        self.assertTrue(outcome["fix_events"], "expected dcos_stale_split_completion_fix_applied")
        self.assertTrue(outcome["c5_complete"])

    @unittest.skipUnless(MILD_A_CONFIG.is_file(), "mild A config missing")
    def test_baseline_repro_start_250_still_submits_c6_without_fix_event(self) -> None:
        outcome = _run_to_c5_sr(250, scaling_config_path=None)
        self.assertTrue(outcome["c5_sr_seen"])
        self.assertTrue(
            outcome["c6_la_on_fill_or_tick"] or outcome["has_c6_in_intent_log"],
        )
        self.assertEqual(outcome["fix_events"], [])


class DcosBaselineIdentitySmokeTests(unittest.TestCase):
    def test_short_backtest_without_dcos_is_stable(self) -> None:
        raw = load_candles_for_symbol("APTUSDT", timeframe="5m", limit=500)
        candles = normalize_candles("APTUSDT", raw)
        kwargs = dict(
            symbol="APTUSDT",
            direction="long",
            candles=candles,
            max_candles=200,
            config_source="live",
            fill_model="conservative",
        )
        first = run_historical_backtest(**kwargs)
        second = run_historical_backtest(**kwargs)
        self.assertEqual(first.final_status, second.final_status)
        self.assertAlmostEqual(first.realized_pnl, second.realized_pnl, places=8)
        self.assertEqual(first.fills_count, second.fills_count)


if __name__ == "__main__":
    unittest.main()
