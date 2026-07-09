from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from research.backtests.recovery_wait_activation import (
    evaluate_recovery_wait_activation,
    load_trade_fill_replay_rows,
    replay_state_at_absolute_index,
)
from research.backtests.run_long_gap_reduction_multi_trade_audit import (
    run_long_gap_reduction_multi_trade_audit,
)


def _candle(ts: datetime, close: float) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "open": close,
        "high": close,
        "low": close,
        "close": close,
    }


class _Candle:
    def __init__(self, ts: datetime, close: float) -> None:
        self.timestamp = ts
        self.open = close
        self.high = close
        self.low = close
        self.close = close


def _write_trade_blocks(path: Path, *, fills: list[dict], start_index: int = 0) -> None:
    path.write_text(
        json.dumps(
            {
                "metadata": {"start_index": start_index},
                "trade_blocks": fills,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _reference_snapshot(*, absolute_index: int, timestamp: str) -> dict:
    return {
        "recovery_candle_index": absolute_index,
        "recovery_fill_timestamp": timestamp,
        "cycle3_candle_index": absolute_index,
        "recovery_fill_price": 100.0,
        "long_qty_at_recovery_start": 10.0,
        "short_qty_at_recovery_start": 5.0,
        "long_avg_at_recovery_start": 100.0,
        "short_avg_at_recovery_start": 100.0,
        "realized_pnl_at_recovery_start": -1.0,
    }


def test_replay_state_uses_last_fill_before_activation(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fills = [
        {
            "row_type": "fill",
            "purpose": "CYCLE_4_LONG_ADD",
            "timestamp": (base + timedelta(minutes=10)).isoformat(),
            "candle_index": 2,
            "global_candle_index": 2,
            "fill_price": 100.0,
            "long_qty_after": 10.0,
            "short_qty_after": 5.0,
            "long_avg_after": 100.0,
            "short_avg_after": 100.0,
            "cumulative_realized_pnl_net": -1.0,
        },
        {
            "row_type": "fill",
            "purpose": "CYCLE_4_SHORT_REDUCE",
            "timestamp": (base + timedelta(minutes=20)).isoformat(),
            "candle_index": 4,
            "global_candle_index": 4,
            "fill_price": 99.0,
            "long_qty_after": 10.0,
            "short_qty_after": 6.0,
            "long_avg_after": 100.0,
            "short_avg_after": 99.5,
            "cumulative_realized_pnl_net": -1.5,
        },
    ]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(blocks_path, fills=fills)
    replay_rows = load_trade_fill_replay_rows(blocks_path, run_start_index=0, input_slice_start_index=0)
    state = replay_state_at_absolute_index(replay_rows, 4)
    assert state is not None
    assert state.short_qty_after == pytest.approx(6.0)
    assert state.cumulative_realized_pnl_net == pytest.approx(-1.5)


def test_original_trade_closed_before_recovery(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0 - i) for i in range(8)]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(
        blocks_path,
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2].timestamp.isoformat(),
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -1.0,
            }
        ],
    )
    run = {
        "trade_number": 1,
        "start_index": 0,
        "end_index": 4,
        "final_status": "closed",
        "end_time": candles[4].timestamp.isoformat(),
        "realized_pnl": 0.5,
    }
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=2,
            timestamp=candles[2].timestamp.isoformat(),
        ),
        trade_blocks_path=blocks_path,
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=3,
        series_last_absolute_index=7,
    )
    assert evaluation.recovery_activated is False
    assert evaluation.non_activation_reason == "original_trade_closed_before_recovery"
    assert evaluation.original_exit_timing == "before_wait_end"


def test_original_trade_closed_on_activation_candle(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0 - i) for i in range(8)]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(
        blocks_path,
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2].timestamp.isoformat(),
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -1.0,
            }
        ],
    )
    run = {
        "trade_number": 1,
        "start_index": 0,
        "end_index": 4,
        "final_status": "closed",
        "end_time": candles[4].timestamp.isoformat(),
        "realized_pnl": 0.5,
    }
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=2,
            timestamp=candles[2].timestamp.isoformat(),
        ),
        trade_blocks_path=blocks_path,
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=2,
        series_last_absolute_index=7,
    )
    assert evaluation.original_exit_absolute_candle_index == 4
    assert evaluation.recovery_activation_absolute_candle_index == 4
    assert evaluation.recovery_activated is False


def test_trade_still_open_after_wait_activates_with_replayed_state(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0 - i) for i in range(10)]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(
        blocks_path,
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2].timestamp.isoformat(),
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 12.0,
                "short_qty_after": 4.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -2.0,
            },
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_SHORT_REDUCE",
                "timestamp": candles[5].timestamp.isoformat(),
                "candle_index": 5,
                "global_candle_index": 5,
                "fill_price": 95.0,
                "long_qty_after": 12.0,
                "short_qty_after": 6.0,
                "long_avg_after": 100.0,
                "short_avg_after": 99.0,
                "cumulative_realized_pnl_net": -2.5,
            },
        ],
    )
    run = {
        "trade_number": 12,
        "start_index": 0,
        "end_index": 9,
        "final_status": "open",
    }
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=2,
            timestamp=candles[2].timestamp.isoformat(),
        ),
        trade_blocks_path=blocks_path,
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=3,
        series_last_absolute_index=9,
    )
    assert evaluation.recovery_activated is True
    assert evaluation.activation_long_qty == pytest.approx(12.0)
    assert evaluation.activation_short_qty == pytest.approx(6.0)
    assert evaluation.activation_gap_qty == pytest.approx(6.0)
    assert evaluation.activation_base_main_realized_pnl == pytest.approx(-2.5)


def test_activation_gap_not_from_reference_snapshot(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0 - i) for i in range(10)]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(
        blocks_path,
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2].timestamp.isoformat(),
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 12.0,
                "short_qty_after": 4.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -2.0,
            },
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_SHORT_REDUCE",
                "timestamp": candles[5].timestamp.isoformat(),
                "candle_index": 5,
                "global_candle_index": 5,
                "fill_price": 95.0,
                "long_qty_after": 12.0,
                "short_qty_after": 8.0,
                "long_avg_after": 100.0,
                "short_avg_after": 99.0,
                "cumulative_realized_pnl_net": -3.0,
            },
        ],
    )
    run = {"trade_number": 1, "start_index": 0, "final_status": "open"}
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=2,
            timestamp=candles[2].timestamp.isoformat(),
        ),
        trade_blocks_path=blocks_path,
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=3,
        series_last_absolute_index=9,
    )
    assert evaluation.activation_gap_qty == pytest.approx(4.0)


def test_series_ended_before_activation(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0) for i in range(5)]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(
        blocks_path,
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2].timestamp.isoformat(),
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -1.0,
            }
        ],
    )
    run = {"trade_number": 1, "start_index": 0, "final_status": "open"}
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=2,
            timestamp=candles[2].timestamp.isoformat(),
        ),
        trade_blocks_path=blocks_path,
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=3,
        series_last_absolute_index=4,
    )
    assert evaluation.recovery_activated is False
    assert evaluation.non_activation_reason == "series_ended_before_activation"


def test_missing_trade_blocks_fail_closed(tmp_path: Path) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0) for i in range(6)]
    run = {"trade_number": 1, "start_index": 0, "final_status": "open"}
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=2,
            timestamp=candles[2].timestamp.isoformat(),
        ),
        trade_blocks_path=tmp_path / "missing.json",
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=1,
        series_last_absolute_index=5,
    )
    assert evaluation.recovery_activated is False
    assert evaluation.non_activation_reason == "activation_state_unavailable"


def _write_wait_mode_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_candle(base + timedelta(minutes=5 * i), 100.0 - i) for i in range(8)]
    candles_path = tmp_path / "candles.json"
    candles_path.write_text(
        json.dumps({"symbol": "TESTUSDT", "timeframe": "5m", "candles": candles}, ensure_ascii=False),
        encoding="utf-8",
    )
    results_path = tmp_path / "continuous_results.json"
    results_path.write_text(
        json.dumps(
            {
                "metadata": {
                    "symbol": "TESTUSDT",
                    "timeframe": "5m",
                    "candles_loaded": 8,
                    "candle_source_total_count": 8,
                    "input_slice_start_index": 0,
                    "index_semantics_version": 2,
                },
                "runs": [
                    {
                        "symbol": "TESTUSDT",
                        "trade_number": 1,
                        "start_index": 0,
                        "start_time": candles[0]["timestamp"],
                        "end_index": 4,
                        "end_time": candles[4]["timestamp"],
                        "final_status": "closed",
                        "realized_pnl": 0.5,
                    },
                    {
                        "symbol": "TESTUSDT",
                        "trade_number": 2,
                        "start_index": 0,
                        "start_time": candles[0]["timestamp"],
                        "final_status": "open",
                    },
                ],
                "aggregate": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_trade_blocks(
        tmp_path / "TESTUSDT_long_continuous_trade_0001_conservative_live_trade_blocks.json",
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2]["timestamp"],
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -1.0,
            }
        ],
    )
    _write_trade_blocks(
        tmp_path / "TESTUSDT_long_continuous_trade_0002_conservative_live_trade_blocks.json",
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[2]["timestamp"],
                "candle_index": 2,
                "global_candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -1.0,
            }
        ],
    )
    return results_path, candles_path, tmp_path


def test_wait_mode_reports_all_trades_and_skips_closed_before_activation(tmp_path: Path) -> None:
    results_path, candles_path, base = _write_wait_mode_fixture(tmp_path)
    out_dir = tmp_path / "out_wait"
    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=2,
    )
    summary = json.loads(outputs["summary_json"].read_text(encoding="utf-8"))
    assert len(summary["trades"]) == 2
    trade1 = next(row for row in summary["trades"] if row["trade_number"] == 1)
    trade2 = next(row for row in summary["trades"] if row["trade_number"] == 2)
    assert trade1["recovery_activated"] is False
    assert trade1["non_activation_reason"] == "original_trade_closed_before_recovery"
    assert trade2["recovery_activated"] is True
    assert trade2["activation_gap_qty"] == pytest.approx(5.0)


@pytest.mark.parametrize("recovery_wait_candles", [0, 144, 288, 576])
def test_wait_candle_values_preserve_activation_index_math(
    tmp_path: Path,
    recovery_wait_candles: int,
) -> None:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = [_Candle(base + timedelta(minutes=5 * i), 100.0) for i in range(1000)]
    blocks_path = tmp_path / "blocks.json"
    _write_trade_blocks(
        blocks_path,
        fills=[
            {
                "row_type": "fill",
                "purpose": "CYCLE_4_LONG_ADD",
                "timestamp": candles[100].timestamp.isoformat(),
                "candle_index": 100,
                "global_candle_index": 100,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_realized_pnl_net": -1.0,
            }
        ],
    )
    run = {"trade_number": 12, "start_index": 0, "final_status": "open"}
    evaluation = evaluate_recovery_wait_activation(
        run=run,
        reference_snapshot=_reference_snapshot(
            absolute_index=100,
            timestamp=candles[100].timestamp.isoformat(),
        ),
        trade_blocks_path=blocks_path,
        candles=candles,
        input_slice_start_index=0,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=recovery_wait_candles,
        series_last_absolute_index=999,
    )
    assert evaluation.recovery_reference_absolute_candle_index == 100
    assert evaluation.recovery_activation_absolute_candle_index == 100 + recovery_wait_candles
    assert evaluation.recovery_wait_minutes == recovery_wait_candles * 5


def test_wait_zero_matches_immediate_c4_long_add(tmp_path: Path) -> None:
    results_path, candles_path, _base = _write_wait_mode_fixture(tmp_path)
    out_immediate = tmp_path / "immediate"
    out_wait0 = tmp_path / "wait0"
    immediate = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_immediate,
        all_eligible_trades=True,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=0,
    )
    wait0 = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_wait0,
        all_eligible_trades=True,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
        recovery_wait_candles=0,
    )
    immediate_trade2 = json.loads(immediate["summary_json"].read_text(encoding="utf-8"))["trades"][0]
    wait0_trade2 = next(
        row
        for row in json.loads(wait0["summary_json"].read_text(encoding="utf-8"))["trades"]
        if row["trade_number"] == 2
    )
    assert immediate_trade2["initial_gap_qty"] == pytest.approx(wait0_trade2["initial_gap_qty"])
    assert immediate_trade2["recovery_end_pnl"] == pytest.approx(wait0_trade2["recovery_end_pnl"])
