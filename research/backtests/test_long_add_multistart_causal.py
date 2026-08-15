"""Tests for causal LONG_ADD multi-start comparison helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.backtest_config_loader import resolve_backtest_config
from research.backtests.backtest_report import BacktestResult
from research.backtests.candle_loader import load_candles_for_symbol
from research.backtests.historical_backtest import run_historical_backtest
from research.backtests.long_add_multistart_metrics import (
    analyze_trade,
    paired_compare_to_baseline,
    same_candle_long_add_short_reduce,
)
from research.backtests.multi_start_backtest import (
    generate_start_indices,
    run_multi_start_backtest,
)
from research.backtests.run_long_add_multistart_causal import resolve_shared_start_indices
from research.backtests.simulated_order_book import SyntheticCandle


def _flat_candles(n: int, *, close: float = 1.0) -> list[SyntheticCandle]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        SyntheticCandle(
            symbol="APTUSDT",
            timestamp=base,
            open=close,
            high=close + 0.01,
            low=close - 0.01,
            close=close,
        )
        for _ in range(n)
    ]


def test_generate_start_indices_require_full_window() -> None:
    indices = generate_start_indices(
        500,
        start_step_candles=100,
        window_candles=200,
        max_starts=20,
        require_full_window=True,
    )
    assert indices == [0, 100, 200, 300]
    assert all(index + 200 <= 500 for index in indices)


def test_shared_start_indices_identical_for_variants() -> None:
    indices, step = resolve_shared_start_indices(50000, window_candles=10000, start_step_candles=250)
    assert step == 250
    assert len(indices) >= 100
    assert len(indices) == len(set(indices))
    assert all(index + 10000 <= 50000 for index in indices)


def test_multi_start_uses_identical_explicit_start_indices() -> None:
    candles = _flat_candles(40)
    starts = [0, 5, 10]
    a = run_multi_start_backtest(
        "APTUSDT",
        "long",
        candles,
        config_source="test",
        window_candles=10,
        start_indices=starts,
        require_full_window=True,
        long_fill_distance_pct=0.5,
    )
    b = run_multi_start_backtest(
        "APTUSDT",
        "long",
        candles,
        config_source="test",
        window_candles=10,
        start_indices=starts,
        require_full_window=True,
        long_fill_distance_pct=1.2,
    )
    assert [row.start_index for row in a] == starts
    assert [row.start_index for row in b] == starts


def test_start_index_zero_is_not_treated_as_missing() -> None:
    """Regression: ``start_index or -1`` would drop index 0."""
    candles = load_candles_for_symbol("APTUSDT", limit=400)
    results = run_multi_start_backtest(
        "APTUSDT",
        "long",
        candles,
        config_source="live",
        fill_model="conservative",
        window_candles=200,
        start_indices=[0, 100],
        require_full_window=True,
        tp_profit_target_pct=0.25,
        long_fill_distance_pct=0.5,
        target_profit_usdt=0.015,
    )
    by_start = {
        int(result.start_index) if result.start_index is not None else -1: result
        for result in results
    }
    assert 0 in by_start
    assert by_start[0].start_index == 0


def test_independent_state_per_start() -> None:
    candles = load_candles_for_symbol("APTUSDT", limit=800)
    starts = [0, 100]
    results = run_multi_start_backtest(
        "APTUSDT",
        "long",
        candles,
        config_source="live",
        fill_model="conservative",
        window_candles=300,
        start_indices=starts,
        require_full_window=True,
        tp_profit_target_pct=0.25,
        long_fill_distance_pct=0.5,
        target_profit_usdt=0.015,
    )
    assert len(results) == 2
    assert results[0].start_index == 0
    assert results[1].start_index == 100
    # Fresh run each window: second start is not a continuation of the first.
    assert results[0].trade_block_id != results[1].trade_block_id
    assert results[0].start_time != results[1].start_time


def test_cli_override_applied_and_live_default_unchanged() -> None:
    before = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    assert float(before.config.long_fill_distance_pct) == 0.5
    candles = load_candles_for_symbol("APTUSDT", limit=80)
    result = run_historical_backtest(
        "APTUSDT",
        "long",
        candles,
        max_candles=20,
        config_source="live",
        fill_model="conservative",
        long_fill_distance_pct=1.2,
        target_profit_usdt=0.015,
        tp_profit_target_pct=0.25,
    )
    assert float(result.long_fill_distance_pct) == 1.2
    after = resolve_backtest_config(config_source="live", signal="long", symbol="APTUSDT")
    assert float(after.config.long_fill_distance_pct) == 0.5
    live_path = Path(before.config_path) if before.config_path else None
    if live_path and live_path.exists():
        text = live_path.read_text(encoding="utf-8")
        assert '"long_fill_distance_pct": 0.5' in text or '"long_fill_distance_pct":0.5' in text


def test_same_candle_helper_detects_violation() -> None:
    fills = [
        {"purpose": "CYCLE_1_LONG_ADD", "candle_index": 10, "fill_price": 1.0, "timestamp": "t"},
        {"purpose": "CYCLE_1_SHORT_REDUCE", "candle_index": 10, "fill_price": 1.1, "timestamp": "t"},
    ]
    assert len(same_candle_long_add_short_reduce(fills)) == 1


def test_mtm_and_paired_comparison() -> None:
    trades_by_variant = {
        0.5: [
            {
                "valid": True,
                "start_index": 0,
                "variant": "la_0_5",
                "status": "open",
                "mtm_pnl": -1.0,
                "duration_candles": 2000,
                "max_abs_net_exposure": 10,
                "max_cycle": 2,
                "negative_closed_trade": 0,
            }
        ],
        0.8: [
            {
                "valid": True,
                "start_index": 0,
                "variant": "la_0_8",
                "status": "closed",
                "mtm_pnl": 0.5,
                "duration_candles": 100,
                "max_abs_net_exposure": 8,
                "max_cycle": 1,
                "negative_closed_trade": 0,
            }
        ],
    }
    paired, summary = paired_compare_to_baseline(trades_by_variant)
    assert len(paired) == 1
    assert paired[0]["mtm_diff_vs_0_5"] == pytest.approx(1.5)
    assert paired[0]["closes_while_baseline_open"] == 1
    assert summary[0]["better_starts"] == 1


def test_smoke_corpus_deterministic(tmp_path: Path) -> None:
    candles = load_candles_for_symbol("APTUSDT", limit=2500)
    starts = generate_start_indices(
        len(candles),
        start_step_candles=500,
        window_candles=800,
        max_starts=3,
        require_full_window=True,
    )
    assert len(starts) >= 2

    def run_once(long_add: float) -> list[dict]:
        results = run_multi_start_backtest(
            "APTUSDT",
            "long",
            candles,
            config_source="live",
            fill_model="conservative",
            window_candles=800,
            start_indices=starts,
            require_full_window=True,
            tp_profit_target_pct=0.25,
            long_fill_distance_pct=long_add,
            target_profit_usdt=0.015,
        )
        rows = []
        for result in results:
            window = candles[int(result.start_index or 0) : int(result.start_index or 0) + 800]
            rows.append(
                analyze_trade(
                    result,
                    variant=f"la_{long_add}",
                    long_add_pct=long_add,
                    target_profit_usdt=0.015,
                    window_candles=window,
                )
            )
        return rows

    first = run_once(0.5)
    second = run_once(0.5)
    assert [row["start_index"] for row in first] == [row["start_index"] for row in second]
    assert [row["mtm_pnl"] for row in first] == [row["mtm_pnl"] for row in second]
    assert [row["status"] for row in first] == [row["status"] for row in second]
    assert all(row["same_candle_long_add_short_reduce"] == 0 for row in first)


def test_analyze_trade_builds_result_object() -> None:
    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        final_status="closed",
        realized_pnl=1.0,
        unrealized_pnl=0.0,
        overall_pnl=1.0,
        fills_count=2,
        candles_processed=10,
        start_index=0,
        window_candles=100,
        entry_price=1.0,
        fill_log=[
            {
                "purpose": "INITIAL_LONG_ENTRY",
                "candle_index": 0,
                "fill_price": 1.0,
                "qty": 1.0,
                "fee_rate": 0.00055,
                "long_qty_after": 1.0,
                "short_qty_after": 0.5,
                "long_avg_after": 1.0,
                "short_avg_after": 1.0,
            }
        ],
    )
    row = analyze_trade(
        result,
        variant="la_0_5",
        long_add_pct=0.5,
        target_profit_usdt=0.015,
    )
    assert row["mtm_pnl"] == pytest.approx(1.0)
    assert row["status"] == "closed"
