"""Tests for backtester trust audit."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from research.backtests.backtester_trust_audit import (
    IndependentLedger,
    audit_trade_blocks,
    result_fingerprint,
    run_determinism_check,
)
from research.backtests.simulated_order_book import SyntheticCandle


def _candle(high: float, low: float, close: float | None = None) -> SyntheticCandle:
    return SyntheticCandle(symbol="APTUSDT", open=close or low, high=high, low=low, close=close or low)


def test_independent_ledger_long_open_and_reduce() -> None:
    ledger = IndependentLedger()
    ledger.apply_fill(side="long", qty=100.0, fill_price=1.0, reduce_only=False)
    assert ledger.long_qty == pytest.approx(100.0)
    assert ledger.long_avg == pytest.approx(1.0)
    details = ledger.apply_fill(side="long", qty=25.0, fill_price=1.1, reduce_only=True)
    assert ledger.long_qty == pytest.approx(75.0)
    assert details["net_pnl"] is not None


def test_independent_ledger_reduce_only_caps_qty() -> None:
    ledger = IndependentLedger()
    ledger.apply_fill(side="short", qty=50.0, fill_price=2.0, reduce_only=False)
    details = ledger.apply_fill(side="short", qty=100.0, fill_price=1.9, reduce_only=True)
    assert ledger.short_qty == pytest.approx(0.0)
    assert details["executed_qty"] == pytest.approx(50.0)


def test_resolve_absolute_candle_index_uses_trade_start_offset() -> None:
    from research.backtests.backtester_trust_audit import _resolve_absolute_candle_index

    row = {"candle_index": 10, "absolute_candle_index": 10}
    assert _resolve_absolute_candle_index(row, start_index=1000) == 1010


def test_audit_trade_blocks_forward_reconciliation() -> None:
    rows = [
        {
            "trade_block_id": "tb-test",
            "direction": "long",
            "row_type": "fill",
            "purpose": "INITIAL_LONG_ENTRY",
            "side": "long",
            "qty": 100.0,
            "fill_price": 1.0,
            "reduce_only": False,
            "closed_pnl": 0.0,
            "confirmed_closed_pnl": 0.0,
            "candle_index": 0,
            "absolute_candle_index": 0,
            "long_qty_after": 100.0,
            "short_qty_after": 0.0,
            "long_avg_after": 1.0,
            "short_avg_after": 0.0,
        },
        {
            "trade_block_id": "tb-test",
            "direction": "long",
            "row_type": "fill",
            "purpose": "INITIAL_SHORT_ENTRY",
            "side": "short",
            "qty": 200.0,
            "fill_price": 1.0,
            "reduce_only": False,
            "closed_pnl": 0.0,
            "confirmed_closed_pnl": 0.0,
            "candle_index": 0,
            "absolute_candle_index": 0,
            "long_qty_after": 100.0,
            "short_qty_after": 200.0,
            "long_avg_after": 1.0,
            "short_avg_after": 1.0,
        },
    ]
    result = {
        "trade_block_id": "tb-test",
        "direction": "long",
        "start_index": 0,
        "end_index": 10,
        "realized_pnl": 0.0,
        "fills_count": 2,
        "final_long_qty": 100.0,
        "final_short_qty": 200.0,
    }
    summary = audit_trade_blocks(rows=rows, result=result, candles=[_candle(1.0, 1.0)])
    assert summary.trusted
    assert summary.checks_failed == 0


def test_audit_detects_position_mismatch() -> None:
    rows = [
        {
            "trade_block_id": "tb-bad",
            "direction": "short",
            "row_type": "fill",
            "purpose": "INITIAL_SHORT_ENTRY",
            "side": "short",
            "qty": 50.0,
            "fill_price": 1.0,
            "reduce_only": False,
            "closed_pnl": 0.0,
            "candle_index": 0,
            "long_qty_after": 0.0,
            "short_qty_after": 999.0,
            "short_avg_after": 1.0,
        }
    ]
    result = {
        "trade_block_id": "tb-bad",
        "direction": "short",
        "start_index": 0,
        "fills_count": 1,
        "realized_pnl": 0.0,
        "final_short_qty": 999.0,
    }
    summary = audit_trade_blocks(rows=rows, result=result, candles=[_candle(1.0, 1.0)])
    assert not summary.trusted
    assert any(f.check_id.startswith("forward_short_qty_") for f in summary.findings)


def test_lifecycle_fill_after_cancel_fails() -> None:
    rows = [
        {
            "row_type": "order",
            "order_id": "ord-1",
            "event_type": "submitted",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "side": "short",
            "qty": 10.0,
            "trigger_price": 1.0,
            "trigger_direction": 2,
            "reduce_only": True,
            "candle_index": 1,
        },
        {
            "row_type": "order",
            "order_id": "ord-1",
            "event_type": "cancelled",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "candle_index": 2,
        },
        {
            "row_type": "fill",
            "order_id": "ord-1",
            "purpose": "CYCLE_1_SHORT_REDUCE",
            "side": "short",
            "qty": 10.0,
            "fill_price": 1.0,
            "reduce_only": True,
            "closed_pnl": 0.1,
            "candle_index": 3,
            "absolute_candle_index": 3,
            "short_qty_after": 90.0,
            "short_avg_after": 1.0,
            "candle_high": 1.0,
            "candle_low": 0.9,
        },
    ]
    candles = [_candle(1.0, 0.9) for _ in range(5)]
    result = {
        "trade_block_id": "tb-lifecycle",
        "direction": "long",
        "start_index": 0,
        "fills_count": 1,
        "realized_pnl": 0.1,
        "final_short_qty": 90.0,
    }
    summary = audit_trade_blocks(rows=rows, result=result, candles=candles)
    assert any(f.check_id.startswith("lifecycle_no_fill_after_cancel_") for f in summary.findings)


def test_result_fingerprint_stable() -> None:
    payload = {"realized_pnl": 1.23, "fills_count": 4, "final_long_qty": 0.0}
    assert result_fingerprint(payload) == result_fingerprint(dict(payload))


@pytest.mark.slow
def test_determinism_check_live_config() -> None:
    from research.backtests.candle_loader import load_candles_for_symbol
    from research.backtests.historical_backtest import normalize_candles

    candles = normalize_candles("APTUSDT", load_candles_for_symbol("APTUSDT", limit=500))
    passed, fp_a, fp_b = run_determinism_check(
        symbol="APTUSDT",
        direction="long",
        candles=candles,
        start_index=0,
        window_candles=300,
    )
    assert passed
    assert fp_a == fp_b


def test_trade_block_export_includes_reduce_only(tmp_path: Path) -> None:
    from research.backtests.backtest_report import BacktestResult
    from research.backtests.trade_block_export import build_trade_block_rows, write_trade_block_exports

    result = BacktestResult(
        symbol="APTUSDT",
        direction="long",
        start_index=0,
        config_source="live",
        fill_model="conservative",
        order_log=[
            {
                "candle_index": 1,
                "event_type": "submitted",
                "order_id": "ord-1",
                "purpose": "LONG_SL_EXIT",
                "side": "long",
                "qty": 10.0,
                "reduce_only": True,
                "trigger_price": 0.9,
            }
        ],
        fill_log=[],
        intent_log=[],
    )
    rows = build_trade_block_rows(result)
    assert any(row.get("reduce_only") is True for row in rows if row.get("row_type") == "order")
    write_trade_block_exports(result, tmp_path)
    exported = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert "reduce_only" in (exported.get("trade_blocks") or [{}])[0]
