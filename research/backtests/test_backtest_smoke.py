"""Phase-1/2/3 smoke tests: original hedge strategies without Bybit."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

from fixed_cycle_hedge_bot.fixed_cycle_strategy import (
    FixedCycleHedgeStrategy,
    ShortFixedCycleHedgeStrategy,
)
from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.candle_loader import (
    DEFAULT_DATA_DIR,
    load_candles,
    load_candles_for_symbol,
    symbol_to_feather_name,
)
from research.backtests.hedge_bot_original_simulator import (
    HedgeBotOriginalSimulator,
    KNOWN_STRUCTURE_PURPOSE_PREFIXES,
    KNOWN_STRUCTURE_PURPOSE_SUFFIXES,
)
from research.backtests.simulated_execution import (
    fill_order_at_price,
    process_candle_fills,
    should_fill_order_on_candle,
)
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle
from research.backtests.simulated_pnl import calculate_simulated_closed_pnl


@pytest.fixture
def long_simulator() -> HedgeBotOriginalSimulator:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    yield sim
    sim.close()


@pytest.fixture
def short_simulator() -> HedgeBotOriginalSimulator:
    sim = HedgeBotOriginalSimulator(signal="short", symbol="BTCUSDT", candle_close=100.0)
    yield sim
    sim.close()


def _assert_entry_intents(result) -> None:
    assert result.entry_intents, "on_start must return initial entry intents on flat snapshot"
    purposes = {intent.purpose for intent in result.entry_intents}
    assert "INITIAL_LONG_ENTRY" in purposes
    assert "INITIAL_SHORT_ENTRY" in purposes
    assert all(intent.qty > 0 for intent in result.entry_intents)


def _assert_entry_fills(result) -> None:
    assert len(result.entry_fills) == 2
    fill_purposes = {fill.purpose for fill in result.entry_fills}
    assert fill_purposes == {"INITIAL_LONG_ENTRY", "INITIAL_SHORT_ENTRY"}
    for fill in result.entry_fills:
        assert fill.status == "FILLED"
        assert fill.exec_qty > 0
        assert fill.exec_price > 0
        assert fill.metadata.get("confirmed_closed_pnl") is not None


def _assert_post_entry_state(result) -> None:
    state = result.strategy_state
    snapshot = result.final_snapshot
    assert snapshot is not None
    assert snapshot.long_qty > 0
    assert snapshot.short_qty > 0
    assert state.get("initial_long_entry_reconciled") is True
    assert state.get("initial_short_entry_reconciled") is True
    assert state.get("entry_reference_price", 0) > 0

    post_purposes = {intent.purpose for intent in result.post_fill_intents}
    has_structure_intents = bool(result.post_fill_intents)
    has_structure_state = any(
        [
            bool(state.get("initial_structure_built")),
            bool(state.get("next_required_purpose")),
            bool(state.get("initial_entry_confirmed")),
            int(state.get("active_cycle_index") or 0) > 0,
        ]
    )
    assert has_structure_intents or has_structure_state, (
        "expected initial structure/cycle/exit intents or updated cycle state after entry fills"
    )
    if has_structure_intents:
        assert any(
            purpose.startswith("CYCLE_") or purpose.endswith("_EXIT")
            for purpose in post_purposes
        )


def _is_structure_purpose(purpose: str) -> bool:
    return purpose.startswith(KNOWN_STRUCTURE_PURPOSE_PREFIXES) or purpose.endswith(
        KNOWN_STRUCTURE_PURPOSE_SUFFIXES
    )


def _assert_active_virtual_orders(result, sim: HedgeBotOriginalSimulator) -> None:
    runtime_state = result.runtime_state
    snapshot = result.final_snapshot
    assert runtime_state is not None
    assert snapshot is not None

    book_orders = sim.book.active_orders()
    assert book_orders, "expected at least one active virtual order after entry smoke"
    assert runtime_state.active_orders, "runtime_state.active_orders must be synced"
    assert snapshot.active_orders, "snapshot must expose active orders via snapshot_from_mapping"

    runtime_purposes = {order.purpose for order in runtime_state.active_orders.values()}
    snapshot_purposes = {order.purpose for order in snapshot.active_orders}
    book_purposes = {order.purpose for order in book_orders}

    assert runtime_purposes == book_purposes
    assert snapshot_purposes == book_purposes
    assert any(_is_structure_purpose(purpose) for purpose in book_purposes)


def test_long_signal_starts_fixed_cycle_strategy_and_builds_structure(long_simulator) -> None:
    assert isinstance(long_simulator.strategy, FixedCycleHedgeStrategy)
    assert not isinstance(long_simulator.strategy, ShortFixedCycleHedgeStrategy)

    result = long_simulator.run_entry_smoke()
    assert result.strategy_name == "FixedCycleHedgeStrategy"
    _assert_entry_intents(result)
    _assert_entry_fills(result)
    _assert_post_entry_state(result)


def test_short_signal_starts_short_fixed_cycle_strategy_and_builds_structure(short_simulator) -> None:
    assert isinstance(short_simulator.strategy, ShortFixedCycleHedgeStrategy)

    result = short_simulator.run_entry_smoke()
    assert result.strategy_name == "ShortFixedCycleHedgeStrategy"
    _assert_entry_intents(result)
    _assert_entry_fills(result)
    _assert_post_entry_state(result)

    first_leg_purpose = short_simulator.strategy._get_first_leg_purpose(1)
    assert first_leg_purpose == "CYCLE_1_SHORT_REDUCE"
    post_purposes = {intent.purpose for intent in result.post_fill_intents}
    if post_purposes:
        assert first_leg_purpose in post_purposes or any(
            p.endswith("_EXIT") for p in post_purposes
        )


def test_long_phase2_active_virtual_orders_in_runtime_and_snapshot(long_simulator) -> None:
    result = long_simulator.run_entry_smoke()
    _assert_active_virtual_orders(result, long_simulator)


def test_short_phase2_active_virtual_orders_in_runtime_and_snapshot(short_simulator) -> None:
    result = short_simulator.run_entry_smoke()
    _assert_active_virtual_orders(result, short_simulator)


def test_cancel_by_purpose_removes_only_matching_order() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})

    intent_a = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        order_type="Limit",
        price=101.0,
        reduce_only=True,
    )
    intent_b = StrategyIntent(
        side="short",
        qty=0.5,
        purpose="SHORT_SL_EXIT",
        order_type="Limit",
        price=99.0,
        reduce_only=True,
    )
    order_a, _ = book.submit_intent(intent_a, replace=False)
    order_b, _ = book.submit_intent(intent_b, replace=False)
    book.sync_runtime_state(runtime_state)

    canceled = book.cancel_by_purpose("LONG_TP_EXIT")
    book.sync_runtime_state(runtime_state)

    assert canceled == [order_a.order_id]
    active_purposes = {order.purpose for order in book.active_orders()}
    assert active_purposes == {"SHORT_SL_EXIT"}
    assert order_b.order_id in runtime_state.active_orders
    assert order_a.order_id not in runtime_state.active_orders


def test_submit_same_purpose_replaces_existing_order() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})

    first = StrategyIntent(
        side="long",
        qty=1.0,
        purpose="LONG_TP_EXIT",
        order_type="Limit",
        price=101.0,
        reduce_only=True,
    )
    second = StrategyIntent(
        side="long",
        qty=2.5,
        purpose="LONG_TP_EXIT",
        order_type="Limit",
        price=102.0,
        reduce_only=True,
    )

    old_order, _ = book.submit_intent(first, replace=True)
    new_order, _ = book.submit_intent(second, replace=True)
    book.sync_runtime_state(runtime_state)

    active = book.active_orders_by_purpose("LONG_TP_EXIT")
    assert len(active) == 1
    assert active[0].order_id == new_order.order_id
    assert active[0].order_id != old_order.order_id
    assert active[0].qty == 2.5
    assert active[0].price == 102.0
    assert book.get_order(old_order.order_id) is not None
    assert book.get_order(old_order.order_id).status == "CANCELED"
    assert len(runtime_state.active_orders) == 1
    assert new_order.order_id in runtime_state.active_orders


def _candle(symbol: str, *, open_: float, high: float, low: float, close: float) -> SyntheticCandle:
    return SyntheticCandle(symbol=symbol, open=open_, high=high, low=low, close=close)


def test_phase3_rise_trigger_fills_when_candle_high_reaches_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            order_type="Market",
            trigger_price=99.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    book.long_qty = 1.0
    book.long_avg = 100.0
    book.short_qty = 0.5
    book.short_avg = 100.0
    book.sync_runtime_state(runtime_state)

    candle = _candle("BTCUSDT", open_=100.0, high=101.0, low=50.0, close=100.0)
    assert should_fill_order_on_candle(order, candle)

    fills, _ = process_candle_fills(book=book, runtime_state=runtime_state, candle=candle)
    assert len(fills) == 1
    assert fills[0].status == "FILLED"
    assert fills[0].purpose == "SHORT_SL_EXIT"
    assert fills[0].metadata.get("trigger_touch_rule") == "high>=trigger"
    assert book.active_orders() == []
    assert order.order_id not in runtime_state.active_orders


def test_phase3_rise_trigger_does_not_fill_when_only_low_reaches_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            order_type="Market",
            trigger_price=99.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    candle = _candle("BTCUSDT", open_=100.0, high=90.0, low=50.0, close=100.0)
    assert not should_fill_order_on_candle(order, candle)
    fills, _ = process_candle_fills(book=book, runtime_state=runtime_state, candle=candle)
    assert fills == []


def test_phase3_sell_order_fills_when_candle_high_reaches_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=101.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    book.long_qty = 1.0
    book.long_avg = 100.0
    book.sync_runtime_state(runtime_state)

    candle = _candle("BTCUSDT", open_=100.0, high=102.0, low=99.0, close=100.0)
    assert should_fill_order_on_candle(order, candle)

    fills, _ = process_candle_fills(book=book, runtime_state=runtime_state, candle=candle)
    assert len(fills) == 1
    assert fills[0].purpose == "LONG_TP_EXIT"
    assert book.active_orders() == []


def test_phase3_orders_stay_active_when_candle_does_not_reach_trigger() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})

    buy_order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            order_type="Limit",
            price=95.0,
            reduce_only=True,
        ),
        replace=False,
    )
    sell_order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Limit",
            price=105.0,
            reduce_only=True,
        ),
        replace=False,
    )
    book.sync_runtime_state(runtime_state)

    candle = _candle("BTCUSDT", open_=100.0, high=102.0, low=98.0, close=100.0)
    fills, _ = process_candle_fills(book=book, runtime_state=runtime_state, candle=candle)

    assert fills == []
    active_ids = {order.order_id for order in book.active_orders()}
    assert buy_order.order_id in active_ids
    assert sell_order.order_id in active_ids
    assert len(runtime_state.active_orders) == 2


def test_phase3_reduce_fill_updates_position_and_pnl() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.long_qty = 1.0
    book.long_avg = 100.0
    order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=101.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    fill = fill_order_at_price(
        book=book,
        runtime_state=runtime_state,
        order_id=order.order_id,
        fill_price=101.0,
    )
    assert book.long_qty == 0.0
    assert fill.metadata.get("confirmed_closed_pnl") == pytest.approx(1.0)
    assert fill.metadata.get("closed_pnl") == pytest.approx(1.0)
    assert fill.metadata.get("runtime_calculated_pnl") == pytest.approx(1.0)


def test_phase35_long_reduce_loss_pnl() -> None:
    pnl, _ = calculate_simulated_closed_pnl(
        side="long",
        avg_entry_price=100.0,
        fill_price=98.0,
        qty=1.0,
        reduce_only=True,
    )
    assert pnl == pytest.approx(-2.0)


def test_phase35_long_reduce_profit_pnl() -> None:
    pnl, _ = calculate_simulated_closed_pnl(
        side="long",
        avg_entry_price=100.0,
        fill_price=103.0,
        qty=1.0,
        reduce_only=True,
    )
    assert pnl == pytest.approx(3.0)


def test_phase35_short_reduce_loss_pnl() -> None:
    pnl, _ = calculate_simulated_closed_pnl(
        side="short",
        avg_entry_price=100.0,
        fill_price=102.0,
        qty=1.0,
        reduce_only=True,
    )
    assert pnl == pytest.approx(-2.0)


def test_phase35_short_reduce_profit_pnl() -> None:
    pnl, _ = calculate_simulated_closed_pnl(
        side="short",
        avg_entry_price=100.0,
        fill_price=97.0,
        qty=1.0,
        reduce_only=True,
    )
    assert pnl == pytest.approx(3.0)


def test_phase35_opening_fill_has_zero_closed_pnl() -> None:
    pnl, details = calculate_simulated_closed_pnl(
        side="long",
        avg_entry_price=100.0,
        fill_price=100.0,
        qty=1.0,
        reduce_only=False,
    )
    assert pnl == pytest.approx(0.0)
    assert details["pnl_calc_source"] == "simulated_opening_zero"


def test_phase35_reduce_fill_metadata_matches_pnl_function() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.short_qty = 1.0
    book.short_avg = 100.0
    order, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=1.0,
            purpose="SHORT_TP_EXIT",
            order_type="Market",
            trigger_price=97.0,
            reduce_only=True,
            metadata={"cycle_index": 1, "cycle_role": "short_reduce"},
        ),
        replace=False,
    )
    expected_pnl, _ = calculate_simulated_closed_pnl(
        side="short",
        avg_entry_price=100.0,
        fill_price=97.0,
        qty=1.0,
        reduce_only=True,
    )
    fill = fill_order_at_price(
        book=book,
        runtime_state=runtime_state,
        order_id=order.order_id,
        fill_price=97.0,
    )
    assert expected_pnl == pytest.approx(3.0)
    assert fill.metadata.get("confirmed_closed_pnl") == pytest.approx(expected_pnl)
    assert fill.metadata.get("closed_pnl") == pytest.approx(expected_pnl)
    assert fill.metadata.get("runtime_calculated_pnl") == pytest.approx(expected_pnl)
    assert fill.metadata.get("cycle_index") == 1
    assert fill.metadata.get("cycle_role") == "short_reduce"
    assert fill.metadata.get("order_id") == order.order_id


def test_phase35_fee_aware_pnl_matches_runtime_formula() -> None:
    fee_rate = 0.00055
    gross = (103.0 - 100.0) * 1.0
    entry_fee = abs(100.0 * 1.0) * fee_rate
    exit_fee = abs(103.0 * 1.0) * fee_rate
    expected_net = gross - entry_fee - exit_fee

    pnl, details = calculate_simulated_closed_pnl(
        side="long",
        avg_entry_price=100.0,
        fill_price=103.0,
        qty=1.0,
        reduce_only=True,
        fee_rate=fee_rate,
    )
    assert pnl == pytest.approx(expected_net)
    assert details["pnl_calc_source"] == "simulated_calculate_pnl_with_fees"
    assert details["gross_pnl"] == pytest.approx(gross)


def test_long_phase3_strategy_integration_fills_active_order(long_simulator) -> None:
    entry_result = long_simulator.run_entry_smoke()
    assert entry_result.resting_orders, "expected resting orders before candle processing"

    before_state = dict(long_simulator.runtime_state.strategy_state)
    active_before = len(long_simulator.book.active_orders())
    assert active_before > 0

    candle = _candle(
        long_simulator.symbol,
        open_=100.0,
        high=102.0,
        low=100.0,
        close=100.5,
    )
    candle_result = long_simulator.process_candle(candle)

    assert candle_result.candle_fills, "expected at least one virtual order fill on trigger candle"
    assert candle_result.snapshot is not None
    assert candle_result.snapshot.active_orders is not None

    after_state = candle_result.strategy_state
    state_changed = after_state != before_state
    has_post_fill_intents = bool(candle_result.on_fill_intents or candle_result.tick_intents)
    has_active_orders = bool(long_simulator.book.active_orders())
    assert state_changed or has_post_fill_intents or has_active_orders


def test_short_phase3_strategy_integration_fills_active_order(short_simulator) -> None:
    entry_result = short_simulator.run_entry_smoke()
    assert entry_result.resting_orders

    candle = _candle(
        short_simulator.symbol,
        open_=100.0,
        high=102.0,
        low=100.0,
        close=100.5,
    )
    candle_result = short_simulator.process_candle(candle)

    assert candle_result.candle_fills
    assert candle_result.snapshot is not None


def test_load_candles_from_temp_csv_last_n_rows() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sample.csv"
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["date", "open", "high", "low", "close", "volume"],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "date": "2026-01-01T00:00:00+00:00",
                    "open": 1,
                    "high": 2,
                    "low": 0.5,
                    "close": 1.5,
                    "volume": 10,
                }
            )
            writer.writerow(
                {
                    "date": "2026-01-01T00:05:00+00:00",
                    "open": 1.5,
                    "high": 2.5,
                    "low": 1.0,
                    "close": 2.0,
                    "volume": 12,
                }
            )

        rows = load_candles(path, limit=1)
        assert len(rows) == 1
        assert rows[0]["close"] == 2.0
        assert rows[0]["volume"] == 12.0
        assert "timestamp" in rows[0]


def test_load_candles_for_symbol_apt_feather_if_available() -> None:
    pytest.importorskip("pyarrow")
    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    rows = load_candles_for_symbol("APTUSDT", limit=5)
    assert len(rows) == 5
    for row in rows:
        assert {"timestamp", "open", "high", "low", "close", "volume"} <= set(row.keys())
        assert row["high"] >= row["low"]
