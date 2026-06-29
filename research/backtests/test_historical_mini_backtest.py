"""Phase-4 historical mini-backtest tests (synthetic-first, APT optional)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fixed_cycle_hedge_bot.models import RuntimeState, StrategyIntent

from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import DEFAULT_DATA_DIR, load_candles_for_symbol, symbol_to_feather_name
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.simulated_execution import process_candle_fills
from research.backtests.simulated_order_book import SimulatedOrderBook, SyntheticCandle


def _make_candles(
    symbol: str,
    closes: list[float],
    *,
    spread: float = 1.0,
) -> list[SyntheticCandle]:
    base_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles: list[SyntheticCandle] = []
    for index, close in enumerate(closes):
        candles.append(
            SyntheticCandle(
                symbol=symbol,
                timestamp=base_time + timedelta(minutes=5 * index),
                open=close,
                high=close + spread,
                low=close - spread,
                close=close,
            )
        )
    return candles


def test_synthetic_mini_backtest_long() -> None:
    candles = _make_candles("BTCUSDT", [100.0, 100.0, 100.5, 101.0, 100.5])
    result = run_historical_backtest(
        "BTCUSDT",
        "long",
        candles,
        max_candles=4,
    )

    assert isinstance(result, BacktestResult)
    assert result.symbol == "BTCUSDT"
    assert result.direction == "long"
    assert result.candles_processed > 0
    assert result.fills_count >= 2
    assert result.final_status in {"open", "closed", "max_candles"}
    assert result.error is None
    assert result.fill_log


def test_synthetic_mini_backtest_short() -> None:
    candles = _make_candles("BTCUSDT", [100.0, 99.5, 99.0, 100.0, 99.5])
    result = run_historical_backtest(
        "BTCUSDT",
        "short",
        candles,
        max_candles=4,
    )

    assert result.direction == "short"
    assert result.candles_processed > 0
    assert result.fills_count >= 2
    assert result.final_status in {"open", "closed", "max_candles"}
    assert result.error is None


def test_max_fills_per_candle_limits_intra_candle_chain() -> None:
    book = SimulatedOrderBook(symbol="BTCUSDT")
    runtime_state = RuntimeState(strategy_state={})
    book.long_qty = 2.0
    book.long_avg = 100.0

    low_tp, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT_A",
            order_type="Market",
            trigger_price=101.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    high_tp, _ = book.submit_intent(
        StrategyIntent(
            side="long",
            qty=1.0,
            purpose="LONG_TP_EXIT_B",
            order_type="Market",
            trigger_price=103.0,
            trigger_direction=1,
            reduce_only=True,
        ),
        replace=False,
    )
    book.sync_runtime_state(runtime_state)

    candle = SyntheticCandle(
        symbol="BTCUSDT",
        open=100.0,
        high=105.0,
        low=99.0,
        close=100.0,
    )

    fills, _ = process_candle_fills(
        book=book,
        runtime_state=runtime_state,
        candle=candle,
        max_fills_per_candle=1,
        conservative_fill_order=True,
    )

    assert len(fills) == 1
    assert len(book.active_orders()) == 1
    filled_ids = {fill.client_order_id for fill in fills}
    remaining_ids = {order.order_id for order in book.active_orders()}
    assert filled_ids.isdisjoint(remaining_ids)
    # Conservative sell ranking: lower trigger (101) fills before higher (103).
    assert low_tp.order_id in filled_ids or high_tp.order_id in filled_ids


def test_backtest_result_to_dict_contains_fill_log() -> None:
    candles = _make_candles("BTCUSDT", [100.0, 100.0, 100.5])
    result = run_historical_backtest("BTCUSDT", "long", candles, max_candles=2)
    payload = result.to_dict()

    assert payload["symbol"] == "BTCUSDT"
    assert payload["direction"] == "long"
    assert isinstance(payload["fill_log"], list)
    assert payload["fills_count"] == len(payload["fill_log"])
    assert payload["final_status"] in {"open", "closed", "max_candles", "error"}


def test_example_synthetic_result_dict_snapshot() -> None:
    candles = _make_candles("BTCUSDT", [100.0, 100.0, 101.0])
    result = run_historical_backtest("BTCUSDT", "long", candles, max_candles=2)
    payload = result.to_dict()

    assert payload["candles_processed"] >= 1
    assert payload["entry_price"] == pytest.approx(100.0)
    assert payload["fills_count"] >= 2
    assert "realized_pnl" in payload
    assert "exit_reason" in payload


def test_aptusdt_mini_backtest_if_data_available() -> None:
    pytest.importorskip("pyarrow")
    apt_path = DEFAULT_DATA_DIR / symbol_to_feather_name("APTUSDT")
    if not apt_path.exists():
        pytest.skip(f"external APT feather file not present: {apt_path}")

    candles = load_candles_for_symbol("APTUSDT", limit=200)
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        candles,
        max_candles=200,
    )

    assert result.error is None
    assert result.candles_processed > 0
    assert result.fills_count >= 2
    assert result.final_status in {"open", "closed", "max_candles"}
