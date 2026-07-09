"""Unit tests for Cycle-3 snapshot derivation from fill_log."""

from __future__ import annotations

import pytest

from research.backtests.backtest_report import BacktestResult
from research.backtests.continuous_reentry_backtest import (
    _compute_cycle3_snapshot_from_fill_log,
    _select_cycle3_fill_from_fill_log,
)


def _fill(
    *,
    purpose: str,
    candle_index: int,
    qty: float = 1.0,
    fill_price: float = 1.9,
    confirmed_closed_pnl: float | None = None,
    closed_pnl: float | None = None,
    order_id: str | None = None,
    order_link_id: str | None = None,
    long_qty_after: float = 39.0,
    short_qty_after: float = 26.0,
    long_avg_after: float = 1.92,
    short_avg_after: float = 1.92,
    fill_price_override: float | None = None,
) -> dict:
    metadata_excerpt: dict = {}
    if order_link_id is not None:
        metadata_excerpt["order_link_id"] = order_link_id
    entry: dict = {
        "purpose": purpose,
        "candle_index": candle_index,
        "timestamp": "2026-01-08T06:10:00+00:00",
        "qty": qty,
        "fill_price": fill_price_override if fill_price_override is not None else fill_price,
        "long_qty_after": long_qty_after,
        "short_qty_after": short_qty_after,
        "long_avg_after": long_avg_after,
        "short_avg_after": short_avg_after,
        "metadata_excerpt": metadata_excerpt,
    }
    if order_id is not None:
        entry["order_id"] = order_id
    if confirmed_closed_pnl is not None:
        entry["confirmed_closed_pnl"] = confirmed_closed_pnl
    if closed_pnl is not None:
        entry["closed_pnl"] = closed_pnl
    return entry


def _result(*fills: dict, start_index: int = 100, input_slice_start_index: int = 0) -> BacktestResult:
    return BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=start_index,
        input_slice_start_index=input_slice_start_index,
        fill_log=list(fills),
    )


def test_cycle3_snapshot_uses_confirmed_zero_not_closed_fallback() -> None:
    result = _result(
        _fill(purpose="CYCLE_1_SHORT_REDUCE", candle_index=10, confirmed_closed_pnl=-1.0),
        _fill(
            purpose="CYCLE_3_SHORT_REDUCE",
            candle_index=20,
            confirmed_closed_pnl=0.0,
            closed_pnl=-5.0,
        ),
    )
    snapshot = _compute_cycle3_snapshot_from_fill_log(result)
    assert snapshot is not None
    assert snapshot["net_realized_pnl_event"] == 0.0
    assert snapshot["cumulative_realized_pnl_net"] == -1.0


def test_cycle3_snapshot_cumulative_through_cycle3_only() -> None:
    result = _result(
        _fill(purpose="INITIAL_SHORT_ENTRY", candle_index=1, confirmed_closed_pnl=0.0),
        _fill(purpose="CYCLE_1_SHORT_REDUCE", candle_index=5, confirmed_closed_pnl=-0.5),
        _fill(purpose="CYCLE_2_SHORT_REDUCE", candle_index=10, confirmed_closed_pnl=-0.3),
        _fill(purpose="CYCLE_3_SHORT_REDUCE", candle_index=15, confirmed_closed_pnl=-0.2),
        _fill(purpose="LONG_TP_EXIT", candle_index=20, confirmed_closed_pnl=1.0),
    )
    snapshot = _compute_cycle3_snapshot_from_fill_log(result)
    assert snapshot is not None
    assert snapshot["cumulative_realized_pnl_net"] == -1.0


def test_cycle3_snapshot_excludes_fills_after_cycle3() -> None:
    result = _result(
        _fill(purpose="CYCLE_3_SHORT_REDUCE", candle_index=15, confirmed_closed_pnl=-0.2),
        _fill(purpose="REFILL_LONG", candle_index=16, confirmed_closed_pnl=0.0),
        _fill(purpose="LONG_TP_EXIT", candle_index=25, confirmed_closed_pnl=2.5),
    )
    snapshot = _compute_cycle3_snapshot_from_fill_log(result)
    assert snapshot is not None
    assert snapshot["cumulative_realized_pnl_net"] == -0.2


def test_cycle3_snapshot_uses_last_partial_fill_for_same_order() -> None:
    fill_log = [
        _fill(
            purpose="CYCLE_3_SHORT_REDUCE",
            candle_index=15,
            qty=2.0,
            confirmed_closed_pnl=-0.1,
            order_id="c3-order-1",
            short_qty_after=24.0,
            short_avg_after=1.91,
        ),
        _fill(
            purpose="CYCLE_3_SHORT_REDUCE",
            candle_index=15,
            qty=4.0,
            confirmed_closed_pnl=-0.2,
            order_id="c3-order-1",
            short_qty_after=20.0,
            short_avg_after=1.90,
        ),
    ]
    selected = _select_cycle3_fill_from_fill_log(fill_log)
    assert selected is fill_log[1]

    result = _result(*fill_log)
    snapshot = _compute_cycle3_snapshot_from_fill_log(result)
    assert snapshot is not None
    assert snapshot["filled_qty"] == 4.0
    assert snapshot["short_qty_after"] == 20.0
    assert snapshot["short_avg_after"] == 1.90
    assert snapshot["cumulative_realized_pnl_net"] == pytest.approx(-0.3)


def test_cycle3_snapshot_groups_by_order_link_id_when_order_id_missing() -> None:
    fill_log = [
        _fill(
            purpose="CYCLE_3_SHORT_REDUCE",
            candle_index=12,
            qty=1.0,
            order_link_id="link-42",
            short_qty_after=25.0,
        ),
        _fill(
            purpose="CYCLE_3_SHORT_REDUCE",
            candle_index=12,
            qty=3.0,
            order_link_id="link-42",
            short_qty_after=22.0,
        ),
    ]
    selected = _select_cycle3_fill_from_fill_log(fill_log)
    assert selected is fill_log[1]


def test_cycle3_snapshot_rejects_zero_filled_qty() -> None:
    result = _result(
        _fill(purpose="CYCLE_3_SHORT_REDUCE", candle_index=15, qty=0.0),
    )
    assert _compute_cycle3_snapshot_from_fill_log(result) is None


def test_cycle3_snapshot_rejects_missing_mandatory_field() -> None:
    entry = _fill(purpose="CYCLE_3_SHORT_REDUCE", candle_index=15)
    entry.pop("fill_price")
    result = _result(entry)
    assert _compute_cycle3_snapshot_from_fill_log(result) is None

    entry2 = _fill(purpose="CYCLE_3_SHORT_REDUCE", candle_index=15)
    entry2.pop("long_qty_after")
    result2 = _result(entry2)
    assert _compute_cycle3_snapshot_from_fill_log(result2) is None


def test_cycle3_snapshot_global_candle_index_from_start_index() -> None:
    result = _result(
        _fill(purpose="CYCLE_3_SHORT_REDUCE", candle_index=42, confirmed_closed_pnl=-0.1),
        start_index=250,
        input_slice_start_index=1000,
    )
    snapshot = _compute_cycle3_snapshot_from_fill_log(result)
    assert snapshot is not None
    assert snapshot["local_candle_index"] == 42
    assert snapshot["slice_candle_index"] == 292
    assert snapshot["absolute_candle_index"] == 1292
    assert snapshot["global_candle_index"] == 1292
