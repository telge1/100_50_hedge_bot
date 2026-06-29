"""Phase-7 fill model tests."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.backtest_report import SUMMARY_CSV_FIELDS
from research.backtests.fill_models import is_exit_purpose, resolve_fill_model_config
from research.backtests.simulated_execution import (
    process_candle_fills,
    select_orders_for_fill_model,
)
from research.backtests.hedge_bot_original_simulator import HedgeBotOriginalSimulator
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.run_original_hedge_backtest import (
    main as cli_main,
    run_fill_model_comparison,
    run_original_hedge_backtests,
)
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


def _touchable_candle(*, high: float, low: float) -> SyntheticCandle:
    return SyntheticCandle(
        symbol="BTCUSDT",
        open=100.0,
        high=high,
        low=low,
        close=100.0,
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _submit_exit_pair(book: SimulatedOrderBook) -> tuple:
    long_tp, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT",
            order_type="Market",
            trigger_price=101.0,
            reduce_only=True,
        ),
        replace=False,
    )
    short_sl, _ = book.submit_intent(
        StrategyIntent(
            side="short",
            qty=0.5,
            purpose="SHORT_SL_EXIT",
            order_type="Market",
            trigger_price=99.0,
            reduce_only=True,
        ),
        replace=False,
    )
    book.long_qty = 1.0
    book.long_avg = 100.0
    book.short_qty = 0.5
    book.short_avg = 100.0
    return long_tp, short_sl


def test_conservative_max_one_fill_from_multiple_touchable() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    _submit_exit_pair(book)
    eligible = list(book.active_orders())
    candle = _touchable_candle(high=102.0, low=98.0)

    fills, stats = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=eligible,
        fill_model="conservative",
        max_fills_per_candle=1,
    )

    assert len(fills) == 1
    assert stats["same_candle_fill_count"] == 1
    assert len(book.active_orders()) == 1


def test_conservative_multi_allows_two_fills_same_candle() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    _submit_exit_pair(book)
    eligible = list(book.active_orders())
    candle = _touchable_candle(high=102.0, low=98.0)

    fills, stats = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=eligible,
        fill_model="conservative_multi",
        max_fills_per_candle=2,
    )

    assert len(fills) == 2
    assert stats["same_candle_fill_count"] == 2
    purposes = {fill.purpose for fill in fills}
    assert purposes == {"LONG_TP_EXIT", "SHORT_SL_EXIT"}


def test_paired_exit_fills_both_exit_orders_same_candle() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    _submit_exit_pair(book)
    eligible = list(book.active_orders())
    candle = _touchable_candle(high=102.0, low=98.0)

    fills, stats = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=eligible,
        fill_model="paired_exit",
        max_fills_per_candle=2,
    )

    assert len(fills) == 2
    assert stats["paired_exit_fills_count"] == 2
    assert {fill.purpose for fill in fills} == {"LONG_TP_EXIT", "SHORT_SL_EXIT"}


def test_new_orders_not_eligible_in_same_candle_snapshot() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    long_tp, short_sl = _submit_exit_pair(book)
    eligible = list(book.active_orders())
    candle = _touchable_candle(high=102.0, low=98.0)

    fills, _ = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=eligible,
        fill_model="conservative",
        max_fills_per_candle=1,
    )
    assert len(fills) == 1

    new_order, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="CYCLE_1_LONG_ADD",
            order_type="Limit",
            price=98.0,
            reduce_only=True,
        ),
        replace=False,
    )
    assert new_order.order_id not in {order.order_id for order in eligible}

    fills_after, _ = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        eligible_orders=eligible,
        fill_model="conservative_multi",
        max_fills_per_candle=2,
    )
    filled_ids = {fill.client_order_id for fill in fills + fills_after}
    assert new_order.order_id not in filled_ids
    assert long_tp.order_id in filled_ids or short_sl.order_id in filled_ids


def test_select_orders_for_fill_model_paired_exit() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    _submit_exit_pair(book)
    candle = _touchable_candle(high=102.0, low=98.0)
    selected, stats = select_orders_for_fill_model(
        list(book.active_orders()),
        candle,
        fill_model="paired_exit",
        max_fills_per_candle=2,
    )
    assert len(selected) == 2
    assert stats["paired_exit_fills_count"] == 2
    assert is_exit_purpose(selected[0].purpose)


def test_historical_backtest_records_fill_model_fields() -> None:
    candles = [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(5)
    ]
    result = run_historical_backtest(
        "BTCUSDT",
        "long",
        candles,
        fill_model="paired_exit",
        max_fills_per_candle=2,
        max_candles=2,
    )
    payload = result.to_dict()
    assert payload["fill_model"] == "paired_exit"
    assert payload["max_fills_per_candle"] == 2


def test_cli_fill_model_writes_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candles = [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(5)
    ]

    def _fake_load(*args, **kwargs):
        return [
            {
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]

    monkeypatch.setattr(
        "research.backtests.run_original_hedge_backtest.load_candles_for_symbol",
        _fake_load,
    )

    exit_code = cli_main(
        [
            "--symbol",
            "BTCUSDT",
            "--direction",
            "long",
            "--limit",
            "5",
            "--max-candles",
            "2",
            "--fill-model",
            "paired_exit",
            "--max-fills-per-candle",
            "2",
            "--output-dir",
            str(tmp_path),
        ]
    )
    assert exit_code == 0
    payload = json.loads((tmp_path / "BTCUSDT_original_hedge_5m_results.json").read_text())
    run = payload["runs"]["long"]
    assert run["fill_model"] == "paired_exit"
    assert run["max_fills_per_candle"] == 2


def test_compare_fill_models_summary_has_all_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candles = [
        SyntheticCandle(
            symbol="BTCUSDT",
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
        )
        for _ in range(5)
    ]

    def _fake_load(*args, **kwargs):
        return [
            {
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]

    monkeypatch.setattr(
        "research.backtests.run_original_hedge_backtest.load_candles_for_symbol",
        _fake_load,
    )

    payload = run_fill_model_comparison(
        symbol="BTCUSDT",
        direction="long",
        limit=5,
        max_candles=2,
        output_dir=tmp_path,
    )
    csv_path = Path(payload["output_files"]["csv"])
    with csv_path.open("r", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    models = {row["fill_model"] for row in rows}
    assert models == {"conservative", "conservative_multi", "paired_exit"}
    assert set(SUMMARY_CSV_FIELDS) <= set(rows[0].keys())


def test_resolve_fill_model_config_defaults() -> None:
    assert resolve_fill_model_config(fill_model="conservative").max_fills_per_candle == 1
    assert resolve_fill_model_config(fill_model="paired_exit").max_fills_per_candle == 2
    assert resolve_fill_model_config(fill_model="paired_exit", max_fills_per_candle=3).max_fills_per_candle == 3


def test_simulator_process_candle_uses_fill_model() -> None:
    sim = HedgeBotOriginalSimulator(signal="long", symbol="BTCUSDT", candle_close=100.0)
    try:
        sim.run_entry_smoke()
        candle = _touchable_candle(high=102.0, low=98.0)
        result = sim.process_candle(
            candle,
            fill_model="conservative",
            max_fills_per_candle=1,
        )
        assert result.same_candle_fill_count <= 1
    finally:
        sim.close()
