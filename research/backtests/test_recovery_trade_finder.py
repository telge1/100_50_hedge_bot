"""Regression tests for recovery trade finder."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.recovery_trade_finder import (
    PRIMARY_RECOVERY_PURPOSE,
    PRIMARY_RECOVERY_WAIT_CANDLES,
    RecoveryScanConfig,
    RecoveryTradeCandidate,
    activation_local_candle_index,
    compute_activation_pnl,
    evaluate_trade_for_recovery,
    fill_rows_from_result,
    filter_viable_start_indices,
    find_reference_fill_row,
    has_final_exit_before_activation,
    load_fill_replay_rows_from_fill_log,
    resolve_absolute_candle_index,
    sort_candidates,
    validate_selected_candidate,
)
from research.backtests.backtest_report import BacktestResult
from research.backtests.recovery_wait_activation import replay_state_at_absolute_index
from research.backtests.simulated_order_book import SyntheticCandle
from research.backtests.simulated_execution import resolve_simulated_fee_rate


def _candle(ts: datetime, close: float) -> SyntheticCandle:
    return SyntheticCandle(
        symbol="TESTUSDT",
        timestamp=ts,
        open=close,
        high=close,
        low=close,
        close=close,
    )


def _fill_row(
    *,
    purpose: str,
    candle_index: int,
    long_qty: float,
    short_qty: float,
    long_avg: float,
    short_avg: float,
    fill_price: float,
    closed_pnl: float = 0.0,
) -> dict:
    return {
        "row_type": "fill",
        "purpose": purpose,
        "candle_index": candle_index,
        "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
        "fill_price": fill_price,
        "closed_pnl": closed_pnl,
        "long_qty_after": long_qty,
        "short_qty_after": short_qty,
        "long_avg_after": long_avg,
        "short_avg_after": short_avg,
    }


def test_activation_local_candle_index_exact_wait() -> None:
    assert activation_local_candle_index(100, 576) == 676


def test_resolve_absolute_candle_index_consistency() -> None:
    assert resolve_absolute_candle_index(local_candle_index=15, start_index=1000) == 1015


def test_find_reference_fill_uses_last_fill_only() -> None:
    rows = [
        _fill_row(
            purpose=PRIMARY_RECOVERY_PURPOSE,
            candle_index=10,
            long_qty=8,
            short_qty=4,
            long_avg=1.0,
            short_avg=1.0,
            fill_price=1.0,
        ),
        _fill_row(
            purpose=PRIMARY_RECOVERY_PURPOSE,
            candle_index=12,
            long_qty=6,
            short_qty=4,
            long_avg=1.0,
            short_avg=1.0,
            fill_price=1.0,
        ),
    ]
    selected = find_reference_fill_row(rows, PRIMARY_RECOVERY_PURPOSE)
    assert selected is not None
    assert selected["candle_index"] == 12


def test_has_final_exit_before_activation_detects_early_exit() -> None:
    rows = [
        _fill_row(
            purpose="LONG_TP_EXIT",
            candle_index=20,
            long_qty=0,
            short_qty=0,
            long_avg=0,
            short_avg=0,
            fill_price=1.1,
        )
    ]
    assert has_final_exit_before_activation(rows, activation_local_candle_index=30)


def test_evaluate_trade_for_recovery_positive_gap() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [
        _candle(base + timedelta(minutes=5 * idx), 1.0 + idx * 0.001)
        for idx in range(800)
    ]
    fill_log = [
        _fill_row(
            purpose="INITIAL_LONG_ENTRY",
            candle_index=0,
            long_qty=10,
            short_qty=0,
            long_avg=1.0,
            short_avg=0,
            fill_price=1.0,
        ),
        _fill_row(
            purpose="INITIAL_SHORT_ENTRY",
            candle_index=0,
            long_qty=10,
            short_qty=5,
            long_avg=1.0,
            short_avg=1.0,
            fill_price=1.0,
        ),
        _fill_row(
            purpose=PRIMARY_RECOVERY_PURPOSE,
            candle_index=50,
            long_qty=8,
            short_qty=5,
            long_avg=1.0,
            short_avg=1.0,
            fill_price=0.99,
            closed_pnl=-0.05,
        ),
    ]
    result = BacktestResult(
        symbol="TESTUSDT",
        direction="long",
        start_index=0,
        trade_number=1,
        trade_block_id="backtest_long_continuous_trade_0001",
        fill_log=fill_log,
    )
    candidate = evaluate_trade_for_recovery(
        result,
        candles=candles,
        scan_config=RecoveryScanConfig(min_follow_candles=10),
        scan_mode="start_index",
        input_slice_start_index=0,
        fee_rate=resolve_simulated_fee_rate(),
    )
    assert candidate.reference_fill_local_candle_index == 50
    assert candidate.activation_local_candle_index == 50 + PRIMARY_RECOVERY_WAIT_CANDLES
    assert candidate.eligible
    assert candidate.gap_at_activation == pytest.approx(3.0)


def test_validate_selected_candidate_reconciles_pnl_and_positions() -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [
        _candle(base + timedelta(minutes=5 * idx), 1.2)
        for idx in range(700)
    ]
    fill_rows = [
        _fill_row(
            purpose=PRIMARY_RECOVERY_PURPOSE,
            candle_index=20,
            long_qty=9,
            short_qty=4,
            long_avg=1.0,
            short_avg=1.0,
            fill_price=1.0,
            closed_pnl=-0.1,
        )
    ]
    activation_local = 20 + PRIMARY_RECOVERY_WAIT_CANDLES
    candidate = RecoveryTradeCandidate(
        scan_mode="start_index",
        trade_block_id="tb-test",
        trade_number=1,
        start_index=0,
        symbol="TESTUSDT",
        direction="long",
        recovery_config_label="primary",
        reference_purpose=PRIMARY_RECOVERY_PURPOSE,
        recovery_wait_candles=PRIMARY_RECOVERY_WAIT_CANDLES,
        eligible=True,
        reference_fill_local_candle_index=20,
        activation_local_candle_index=activation_local,
        activation_absolute_candle_index=activation_local,
        long_qty_at_activation=9,
        short_qty_at_activation=4,
        long_avg_at_activation=1.0,
        short_avg_at_activation=1.0,
        gap_at_activation=5.0,
        realized_pnl_net=-0.1,
        candles_remaining_after_activation=103,
    )
    replay_rows = load_fill_replay_rows_from_fill_log(
        fill_rows,
        run_start_index=0,
        input_slice_start_index=0,
    )
    replay_state = replay_state_at_absolute_index(replay_rows, activation_local)
    assert replay_state is not None
    pnl = compute_activation_pnl(
        replay_state=replay_state,
        reference_price=1.2,
        fee_rate=resolve_simulated_fee_rate(),
    )
    candidate.total_net_pnl_if_closed_at_activation = pnl["total_net_pnl_if_closed_at_activation"]
    errors = validate_selected_candidate(
        candidate,
        fill_rows=fill_rows,
        candles=candles,
        input_slice_start_index=0,
        fee_rate=resolve_simulated_fee_rate(),
    )
    assert not errors


def test_filter_viable_start_indices_respects_tail_room() -> None:
    indices = filter_viable_start_indices([0, 100, 52000], candle_count=52569)
    assert 0 in indices
    assert 100 in indices
    assert 52000 not in indices


def test_sort_candidates_prefers_eligible_largest_gap() -> None:
    low = RecoveryTradeCandidate(
        scan_mode="start_index",
        trade_block_id="a",
        trade_number=1,
        start_index=0,
        symbol="APTUSDT",
        direction="long",
        recovery_config_label="primary",
        reference_purpose=PRIMARY_RECOVERY_PURPOSE,
        recovery_wait_candles=576,
        eligible=True,
        gap_at_activation=1.0,
        candles_remaining_after_activation=100,
    )
    high = RecoveryTradeCandidate(
        scan_mode="start_index",
        trade_block_id="b",
        trade_number=2,
        start_index=100,
        symbol="APTUSDT",
        direction="long",
        recovery_config_label="primary",
        reference_purpose=PRIMARY_RECOVERY_PURPOSE,
        recovery_wait_candles=576,
        eligible=True,
        gap_at_activation=5.0,
        candles_remaining_after_activation=50,
    )
    rejected = RecoveryTradeCandidate(
        scan_mode="start_index",
        trade_block_id="c",
        trade_number=3,
        start_index=200,
        symbol="APTUSDT",
        direction="long",
        recovery_config_label="primary",
        reference_purpose=PRIMARY_RECOVERY_PURPOSE,
        recovery_wait_candles=576,
        eligible=False,
        gap_at_activation=10.0,
        candles_remaining_after_activation=200,
    )
    ordered = sort_candidates([low, rejected, high])
    assert ordered[0].trade_block_id == "b"
    assert ordered[-1].trade_block_id == "c"


def test_fill_rows_from_result_accumulates_net_pnl() -> None:
    result = BacktestResult(
        symbol="TESTUSDT",
        direction="long",
        fill_log=[
            {"purpose": "X", "candle_index": 1, "closed_pnl": 0.1},
            {"purpose": "Y", "candle_index": 2, "closed_pnl": -0.2},
        ],
    )
    rows = fill_rows_from_result(result)
    assert rows[-1]["cumulative_realized_pnl_net"] == pytest.approx(-0.1)
