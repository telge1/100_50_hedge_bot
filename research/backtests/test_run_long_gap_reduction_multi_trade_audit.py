from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from research.backtests.run_long_gap_reduction_multi_trade_audit import (
    run_long_gap_reduction_multi_trade_audit,
)


def _write_candles_json(path: Path) -> None:
    ts0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    prices = [100.0, 99.0, 98.01, 97.0299, 96.059601]
    for i, p in enumerate(prices):
        candles.append(
            {
                "timestamp": (ts0).isoformat(),
                "open": p,
                "high": p,
                "low": p,
                "close": p,
            }
        )
    payload = {
        "symbol": "TESTUSDT",
        "timeframe": "5m",
        "candles": candles,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_continuous_results_with_embedded_snapshot(path: Path) -> None:
    payload = {
        "metadata": {
            "symbol": "TESTUSDT",
            "timeframe": "5m",
            "candles_loaded": 5,
            "candle_source_total_count": 5,
            "input_slice_start_index": 0,
            "index_semantics_version": 2,
        },
        "runs": [
            {
                "symbol": "TESTUSDT",
                "direction": "long",
                "trade_number": 1,
                "start_index": 0,
                "end_index": 4,
                "realized_pnl": 0.0,
                "cycle3_snapshot": {
                    "purpose": "CYCLE_3_SHORT_REDUCE",
                    "local_candle_index": 0,
                    "slice_candle_index": 0,
                    "absolute_candle_index": 0,
                    "global_candle_index": 0,
                    "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                    "fill_price": 100.0,
                    "filled_qty": 5.0,
                    "fee_rate": 0.00055,
                    "entry_fee": 0.0,
                    "exit_fee": 0.0,
                    "closing_fee": 0.0,
                    "gross_realized_pnl_event": -5.0,
                    "net_realized_pnl_event": -5.0,
                    "cumulative_realized_pnl_net": -5.0,
                    "long_qty_after": 10.0,
                    "short_qty_after": 5.0,
                    "long_avg_after": 100.0,
                    "short_avg_after": 100.0,
                },
            }
        ],
        "aggregate": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_dry_run_only_writes_preflight(tmp_path: Path) -> None:
    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"

    _write_continuous_results_with_embedded_snapshot(results_path)
    _write_candles_json(candles_path)

    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        dry_run=True,
    )

    preflight_path = outputs["preflight_path"]
    assert preflight_path.exists()

    payload = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["eligible_trade_numbers"] == [1]
    assert payload["eligible_count"] == 1
    assert payload["ineligible_trades"] == []

    # In dry-run mode no simulation outputs should be written.
    assert not (out_dir / "long_gap_reduction_multi_trade_orders.csv").exists()
    assert not (out_dir / "long_gap_reduction_multi_trade_events.csv").exists()
    assert not (out_dir / "long_gap_reduction_multi_trade_summary.csv").exists()
    assert not (out_dir / "long_gap_reduction_multi_trade_summary.json").exists()


def test_full_run_writes_all_outputs(tmp_path: Path) -> None:
    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"

    _write_continuous_results_with_embedded_snapshot(results_path)
    _write_candles_json(candles_path)

    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        dry_run=False,
    )

    summary_csv = outputs["summary_csv"]
    summary_json = outputs["summary_json"]

    assert summary_csv.exists()
    assert summary_json.exists()

    summary_payload = json.loads(summary_json.read_text(encoding="utf-8"))
    assert summary_payload["run_id"] == outputs["run_id"]
    trades = summary_payload["trades"]
    assert len(trades) == 1

    trade = trades[0]
    # Gap should be initial_long - initial_short = 5.0
    assert trade["initial_gap_qty"] == pytest.approx(5.0)
    # Planned per-step reduction is gap/4.
    assert trade["planned_gap_reduce_qty_per_step"] == pytest.approx(1.25)


def _write_continuous_results_without_snapshot(path: Path) -> None:
    payload = {
        "metadata": {
            "symbol": "TESTUSDT",
            "timeframe": "5m",
            "candles_loaded": 5,
            "candle_source_total_count": 5,
            "input_slice_start_index": 0,
        },
        "runs": [
            {
                "symbol": "TESTUSDT",
                "direction": "long",
                "trade_number": 1,
                "start_index": 0,
                "end_index": 4,
                "realized_pnl": 0.0,
            }
        ],
        "aggregate": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_ineligible_when_no_cycle3_snapshot(tmp_path: Path) -> None:
    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"

    _write_continuous_results_without_snapshot(results_path)
    _write_candles_json(candles_path)

    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        dry_run=True,
    )

    preflight_path = outputs["preflight_path"]
    payload = json.loads(preflight_path.read_text(encoding="utf-8"))

    assert payload["eligible_trade_numbers"] == []
    assert payload["eligible_count"] == 0
    assert len(payload["ineligible_trades"]) == 1
    entry = payload["ineligible_trades"][0]
    assert entry["trade_number"] == 1
    # With no embedded fields and no trade-blocks JSON we expect a clear reason.
    assert entry["reason"] in {
        "trade_blocks_file_not_found",
        "cycle3_fill_not_found",
        "recovery_fill_not_found",
    }


def test_legacy_slice_relative_global_index_resolves_to_absolute(tmp_path: Path) -> None:
    from datetime import timedelta

    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index in range(10):
        ts = base + timedelta(minutes=5 * index)
        candles.append(
            {
                "timestamp": ts.isoformat(),
                "open": 100.0 + index,
                "high": 100.0 + index,
                "low": 100.0 + index,
                "close": 100.0 + index,
            }
        )
    candles_path.write_text(
        json.dumps({"symbol": "TESTUSDT", "timeframe": "5m", "candles": candles}, ensure_ascii=False),
        encoding="utf-8",
    )

    fill_timestamp = candles[6]["timestamp"]
    payload = {
        "metadata": {
            "symbol": "TESTUSDT",
            "candles_loaded": 5,
            "candle_source_total_count": 10,
        },
        "runs": [
            {
                "symbol": "TESTUSDT",
                "direction": "long",
                "trade_number": 1,
                "start_index": 0,
                "cycle3_snapshot": {
                    "purpose": "CYCLE_3_SHORT_REDUCE",
                    "local_candle_index": 1,
                    "global_candle_index": 1,
                    "timestamp": fill_timestamp,
                    "fill_price": 106.0,
                    "filled_qty": 1.0,
                    "long_qty_after": 10.0,
                    "short_qty_after": 5.0,
                    "long_avg_after": 100.0,
                    "short_avg_after": 100.0,
                    "cumulative_realized_pnl_net": -1.0,
                },
            }
        ],
        "aggregate": [],
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        dry_run=True,
    )
    preflight = json.loads(outputs["preflight_path"].read_text(encoding="utf-8"))
    assert preflight["index_resolution"]["input_slice_start_index"] == 5
    assert preflight["eligible_count"] == 1
    assert preflight["eligible_trade_numbers"] == [1]


def _write_trade_blocks_for_purpose(
    path: Path,
    *,
    purpose: str,
    timestamp: str,
) -> None:
    payload = {
        "metadata": {"start_index": 0},
        "trade_blocks": [
            {
                "row_type": "fill",
                "purpose": purpose,
                "timestamp": timestamp,
                "candle_index": 2,
                "fill_price": 98.0,
                "long_qty_after": 10.0,
                "short_qty_after": 5.0,
                "long_avg_after": 100.0,
                "short_avg_after": 100.0,
                "cumulative_pnl": -1.0,
                "cycle_index": 4,
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_default_recovery_start_purpose_is_cycle3_short_reduce(tmp_path: Path) -> None:
    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"
    _write_continuous_results_with_embedded_snapshot(results_path)
    _write_candles_json(candles_path)
    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        dry_run=True,
    )
    preflight = json.loads(outputs["preflight_path"].read_text(encoding="utf-8"))
    assert preflight["recovery_start_purpose"] == "CYCLE_3_SHORT_REDUCE"


def test_recovery_start_after_cycle4_long_add_from_trade_blocks(tmp_path: Path) -> None:
    from datetime import timedelta

    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    candles = []
    for index in range(5):
        ts = base + timedelta(minutes=5 * index)
        candles.append(
            {
                "timestamp": ts.isoformat(),
                "open": 100.0 - index,
                "high": 100.0 - index,
                "low": 100.0 - index,
                "close": 100.0 - index,
            }
        )
    candles_path.write_text(
        json.dumps({"symbol": "TESTUSDT", "timeframe": "5m", "candles": candles}, ensure_ascii=False),
        encoding="utf-8",
    )
    payload = {
        "metadata": {
            "symbol": "TESTUSDT",
            "candles_loaded": 5,
            "candle_source_total_count": 5,
            "input_slice_start_index": 0,
        },
        "runs": [
            {
                "symbol": "TESTUSDT",
                "direction": "long",
                "trade_number": 1,
                "start_index": 0,
                "start_time": candles[0]["timestamp"],
            }
        ],
        "aggregate": [],
    }
    results_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _write_trade_blocks_for_purpose(
        tmp_path / "TESTUSDT_long_continuous_trade_0001_conservative_live_trade_blocks.json",
        purpose="CYCLE_4_LONG_ADD",
        timestamp=candles[2]["timestamp"],
    )

    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        all_eligible_trades=True,
        recovery_start_purpose="CYCLE_4_LONG_ADD",
    )
    summary = json.loads(outputs["summary_json"].read_text(encoding="utf-8"))
    assert summary["recovery_start_purpose"] == "CYCLE_4_LONG_ADD"
    assert len(summary["trades"]) == 1
    trade = summary["trades"][0]
    assert trade["recovery_start_timestamp"] == candles[2]["timestamp"]
    assert trade["minutes_trade_start_to_recovery"] is not None


def test_missing_recovery_trigger_purpose_stays_ineligible(tmp_path: Path) -> None:
    results_path = tmp_path / "continuous_results.json"
    candles_path = tmp_path / "candles.json"
    out_dir = tmp_path / "out"
    _write_continuous_results_without_snapshot(results_path)
    _write_candles_json(candles_path)
    outputs = run_long_gap_reduction_multi_trade_audit(
        input_results=results_path,
        input_candles=candles_path,
        output_dir=out_dir,
        dry_run=True,
        recovery_start_purpose="CYCLE_4_SHORT_REDUCE",
    )
    preflight = json.loads(outputs["preflight_path"].read_text(encoding="utf-8"))
    assert preflight["eligible_count"] == 0
    assert preflight["ineligible_trades"][0]["reason"] in {
        "recovery_fill_not_found",
        "trade_blocks_file_not_found",
    }

