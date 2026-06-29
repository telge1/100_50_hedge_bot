"""Tests for backtest-only cycle fill reference repair."""

from __future__ import annotations

import pytest

from fixed_cycle_hedge_bot.models import FillEvent

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.cycle_fill_reference_repair import (
    _repair_vwap_fill_entry,
    install_cycle_fill_reference_repair,
    repair_cycle_fill_maps_after_advance,
)
from research.backtests.hedge_bot_original_simulator import (
    HedgeBotOriginalSimulator,
    build_runtime_state,
    build_strategy,
)
from research.backtests.historical_backtest import normalize_candles, run_historical_backtest


def test_repair_vwap_fill_entry_fixes_doubled_total_qty() -> None:
    entry = {
        "price": 0.29875,
        "qty": 20.664,
        "total_qty": 41.328,
        "weighted_price_sum": 12.346740000000002,
        "avg_price": 0.29875,
    }
    repaired = _repair_vwap_fill_entry(entry)
    assert repaired["total_qty"] == pytest.approx(20.664)
    assert repaired["avg_price"] == pytest.approx(0.5975)
    assert repaired["price"] == pytest.approx(0.5975)


def test_apt_long_start800_cycle2_trigger_after_short_reduce_fill() -> None:
    candles = normalize_candles(
        "APTUSDT",
        load_candles_for_symbol("APTUSDT", limit=900)[800:900],
    )
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        candles,
        max_candles=98,
        config_source="live",
        fill_model="conservative",
        max_fills_per_candle=1,
    )
    short_reduce_fill = next(
        item["fill_price"]
        for item in result.fill_log
        if item.get("purpose") == "CYCLE_1_SHORT_REDUCE"
    )
    cycle2_intents = [
        item
        for item in result.intent_log
        if item.get("purpose") == "CYCLE_2_LONG_ADD"
    ]
    assert cycle2_intents, "expected CYCLE_2_LONG_ADD intent"
    trigger = float(cycle2_intents[0]["trigger_price"])
    expected = float(short_reduce_fill) * 0.995
    assert trigger == pytest.approx(expected, rel=0, abs=0.0002)
    assert trigger != pytest.approx(short_reduce_fill * 0.5, rel=0, abs=0.0002)


def test_force_commit_does_not_halve_confirmed_short_reduce_reference() -> None:
    candles = normalize_candles(
        "APTUSDT",
        load_candles_for_symbol("APTUSDT", limit=900)[800:900],
    )
    config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    sim = HedgeBotOriginalSimulator(
        signal="long",
        symbol="APTUSDT",
        candle_close=float(candles[0].close),
        config_load=config_load,
    )
    sim.run_entry_smoke()
    for index, candle in enumerate(candles[1:], start=1):
        sim.candle_index = index
        candle_result = sim.process_candle(
            candle,
            fill_model="conservative",
            max_fills_per_candle=1,
        )
        for fill in candle_result.candle_fills:
            if fill.purpose != "CYCLE_1_SHORT_REDUCE":
                continue
            entry = sim.strategy._get_cycle_sequence_entry(sim.runtime_state, 1)
            assert float(entry["short_reduce_fill_price"]) == pytest.approx(float(fill.exec_price))
            assert float(entry["short_reduce_fill_price"]) != pytest.approx(float(fill.exec_price) * 0.5)
            return
    pytest.fail("CYCLE_1_SHORT_REDUCE fill not observed")


def test_repair_cycle_fill_maps_after_advance_syncs_sequence_entry() -> None:
    config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    sim = HedgeBotOriginalSimulator(
        signal="long",
        symbol="APTUSDT",
        candle_close=0.6,
        config_load=config_load,
    )
    strategy = sim.strategy
    runtime = sim.runtime_state
    cycle_state = strategy._ensure_cycle_state(runtime)
    cycle_state["short_fills"] = {
        "1": {
            "price": 0.29875,
            "qty": 20.664,
            "total_qty": 41.328,
            "weighted_price_sum": 12.346740000000002,
            "avg_price": 0.29875,
        }
    }
    fill_event = FillEvent(
        exchange_order_id="sim-ex-test",
        client_order_id="sim-fixed_cycle-test-1",
        side="short",
        purpose="CYCLE_1_SHORT_REDUCE",
        exec_qty=20.664,
        exec_price=0.5975,
        order_type="Market",
        reduce_only=True,
        status="FILLED",
    )
    repair_cycle_fill_maps_after_advance(strategy, runtime, fill_event)
    entry = strategy._get_cycle_sequence_entry(runtime, 1)
    assert float(entry["short_reduce_fill_price"]) == pytest.approx(0.5975)
    assert bool(entry["short_reduce_fill_confirmed"]) is True


def test_short_side_long_reduce_reference_repair() -> None:
    config_load = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT")
    strategy = build_strategy("short", config_load.config)
    install_cycle_fill_reference_repair(strategy)
    runtime = build_runtime_state(symbol="APTUSDT", price_tick_size=config_load.config.price_tick_size)
    sim = HedgeBotOriginalSimulator(
        signal="short",
        symbol="APTUSDT",
        candle_close=0.6,
        config_load=config_load,
    )
    strategy = sim.strategy
    runtime = sim.runtime_state
    cycle_state = strategy._ensure_cycle_state(runtime)
    cycle_state["long_fills"] = {
        "1": {
            "price": 0.29875,
            "qty": 20.664,
            "total_qty": 41.328,
            "weighted_price_sum": 12.346740000000002,
            "avg_price": 0.29875,
        }
    }
    fill_event = FillEvent(
        exchange_order_id="sim-ex-test",
        client_order_id="sim-fixed_cycle-test-1",
        side="long",
        purpose="CYCLE_1_LONG_REDUCE",
        exec_qty=20.664,
        exec_price=0.5975,
        order_type="Market",
        reduce_only=True,
        status="FILLED",
    )
    repair_cycle_fill_maps_after_advance(strategy, runtime, fill_event)
    entry = strategy._get_cycle_sequence_entry(runtime, 1)
    assert float(entry["long_reduce_fill_price"]) == pytest.approx(0.5975)


def test_recovery_mode_override_pct_inactive_in_live_config() -> None:
    config_load = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    assert config_load.config.recovery_mode_trigger_override_enabled is False
    assert float(config_load.config.recovery_mode_trigger_override_pct or 0.0) == 50.0
    candles = normalize_candles(
        "APTUSDT",
        load_candles_for_symbol("APTUSDT", limit=900)[800:900],
    )
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        candles,
        max_candles=98,
        config_source="live",
        fill_model="conservative",
        max_fills_per_candle=1,
    )
    short_reduce_fill = next(
        item["fill_price"]
        for item in result.fill_log
        if item.get("purpose") == "CYCLE_1_SHORT_REDUCE"
    )
    trigger = float(
        next(
            item["trigger_price"]
            for item in result.intent_log
            if item.get("purpose") == "CYCLE_2_LONG_ADD"
        )
    )
    half_distance_trigger = float(short_reduce_fill) * 0.5
    normal_distance_trigger = float(short_reduce_fill) * 0.995
    assert trigger == pytest.approx(normal_distance_trigger, rel=0, abs=0.0002)
    assert trigger != pytest.approx(half_distance_trigger, rel=0, abs=0.0002)


def test_install_is_idempotent() -> None:
    config_load = resolve_backtest_config(config_source="live", signal="short", symbol="APTUSDT")
    strategy = build_strategy("short", config_load.config)
    install_cycle_fill_reference_repair(strategy)
    first_commit = strategy._commit_short_reduce_terminal_fill
    install_cycle_fill_reference_repair(strategy)
    assert strategy._commit_short_reduce_terminal_fill is first_commit
