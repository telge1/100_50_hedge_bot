"""Tests for candle index resolution between backtest slices and full feather files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from research.backtests.candle_index_resolution import (
    compute_input_slice_start_index,
    exact_timestamp_match_index,
    index_diagnostics_for_candle,
    resolve_absolute_candle_index,
    resolve_input_slice_start_index,
)


class _Candle:
    def __init__(self, timestamp: datetime) -> None:
        self.timestamp = timestamp


def _timestamps(count: int) -> list[datetime]:
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [base + timedelta(minutes=5 * index) for index in range(count)]


def test_compute_input_slice_start_index_for_tail_slice() -> None:
    assert compute_input_slice_start_index(total_candle_count=52569, slice_candle_count=50000) == 2569


def test_resolve_input_slice_start_index_from_metadata_counts() -> None:
    timestamps = _timestamps(52569)
    resolution, error = resolve_input_slice_start_index(
        {
            "candles_loaded": 50000,
            "candle_source_total_count": 52569,
        },
        timestamps,
    )
    assert error is None
    assert resolution is not None
    assert resolution.input_slice_start_index == 2569
    assert resolution.resolution_source == "metadata_computed_from_candles_loaded_and_total"


def test_resolve_input_slice_start_index_from_full_file_legacy() -> None:
    timestamps = _timestamps(52569)
    resolution, error = resolve_input_slice_start_index(
        {"candles_loaded": 50000},
        timestamps,
    )
    assert error is None
    assert resolution is not None
    assert resolution.input_slice_start_index == 2569
    assert resolution.resolution_source == (
        "legacy_computed_from_full_candle_file_and_candles_loaded"
    )


def test_resolve_input_slice_start_index_from_exact_timestamp_legacy() -> None:
    timestamps = _timestamps(10)
    target = timestamps[4]
    resolution, error = resolve_input_slice_start_index(
        {"input_slice_first_timestamp": target.isoformat()},
        timestamps,
    )
    assert error is None
    assert resolution is not None
    assert resolution.input_slice_start_index == 4
    assert resolution.resolution_source == "legacy_exact_slice_start_timestamp_match"


def test_resolve_input_slice_start_index_rejects_non_unique_timestamp() -> None:
    timestamps = _timestamps(3)
    timestamps[2] = timestamps[0]
    resolution, error = resolve_input_slice_start_index(
        {"input_slice_first_timestamp": timestamps[0].isoformat()},
        timestamps,
    )
    assert resolution is None
    assert error == "timestamp_not_unique_in_candle_file"


def test_resolve_input_slice_start_index_rejects_missing_slice_start() -> None:
    resolution, error = resolve_input_slice_start_index({}, [])
    assert resolution is None
    assert error == "input_slice_start_index_not_resolvable"


def test_resolve_absolute_candle_index_adds_slice_offset_for_legacy_global() -> None:
    resolved = resolve_absolute_candle_index(
        stored_local_candle_index=65,
        stored_slice_candle_index=None,
        stored_global_candle_index=2669,
        input_slice_start_index=2569,
        index_resolution_source="legacy_slice_relative_global_candle_index",
    )
    assert resolved is not None
    assert resolved.stored_slice_candle_index == 2669
    assert resolved.resolved_global_candle_index == 5238
    assert resolved.index_offset == 2569


def test_index_diagnostics_matches_fill_timestamp_at_resolved_index() -> None:
    timestamps = _timestamps(52569)
    candles = [_Candle(ts) for ts in timestamps]
    fill_ts = timestamps[5238]
    diag = index_diagnostics_for_candle(
        candles=candles,
        stored_local_candle_index=65,
        stored_slice_candle_index=2669,
        stored_global_candle_index=2669,
        input_slice_start_index=2569,
        slice_resolution_source="legacy_slice_relative_global_candle_index",
        cycle3_fill_timestamp=fill_ts.isoformat(),
    )
    assert diag["resolved_global_candle_index"] == 5238
    assert diag["index_offset"] == 2569
    assert diag["timestamp_matches_candle"] is True


def test_index_diagnostics_rejects_timestamp_mismatch() -> None:
    timestamps = _timestamps(100)
    candles = [_Candle(ts) for ts in timestamps]
    diag = index_diagnostics_for_candle(
        candles=candles,
        stored_local_candle_index=10,
        stored_slice_candle_index=20,
        stored_global_candle_index=20,
        input_slice_start_index=5,
        slice_resolution_source="legacy_slice_relative_global_candle_index",
        cycle3_fill_timestamp=timestamps[99].isoformat(),
    )
    assert diag["resolved_global_candle_index"] == 25
    assert diag["timestamp_matches_candle"] is False


def test_exact_timestamp_match_index_requires_single_match() -> None:
    timestamps = _timestamps(5)
    matched, error = exact_timestamp_match_index(timestamps, timestamps[2])
    assert error is None
    assert matched == 2

    matched_dup, error_dup = exact_timestamp_match_index(timestamps, timestamps[2])
    timestamps_with_dup = list(timestamps)
    timestamps_with_dup[4] = timestamps[2]
    matched_dup, error_dup = exact_timestamp_match_index(timestamps_with_dup, timestamps[2])
    assert matched_dup is None
    assert error_dup == "timestamp_not_unique_in_candle_file"
