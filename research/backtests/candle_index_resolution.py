"""Resolve candle index semantics between backtest input slices and full candle files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence


@dataclass(frozen=True)
class InputSliceStartResolution:
    input_slice_start_index: int
    resolution_source: str
    input_slice_first_timestamp: str | None = None


@dataclass(frozen=True)
class ResolvedCandleIndex:
    stored_local_candle_index: int | None
    stored_slice_candle_index: int | None
    input_slice_start_index: int
    resolved_global_candle_index: int
    index_offset: int
    index_resolution_source: str


def compute_input_slice_start_index(
    *,
    total_candle_count: int,
    slice_candle_count: int,
) -> int:
    """Return the absolute feather index of the first candle in a tail slice."""
    total = int(total_candle_count)
    loaded = int(slice_candle_count)
    if loaded <= 0 or loaded >= total:
        return 0
    return total - loaded


def exact_timestamp_match_index(
    timestamps: Sequence[datetime | None],
    target: datetime,
) -> tuple[int | None, str | None]:
    matches = [index for index, ts in enumerate(timestamps) if ts is not None and ts == target]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, "timestamp_not_found_in_candle_file"
    return None, "timestamp_not_unique_in_candle_file"


def resolve_input_slice_start_index(
    continuous_metadata: dict[str, Any],
    full_candle_timestamps: Sequence[datetime | None],
) -> tuple[InputSliceStartResolution | None, str | None]:
    """
    Resolve where the backtest input slice starts in the full candle file.

    Allowed sources (in order):
    1. explicit metadata.input_slice_start_index
    2. metadata.candle_source_total_count + metadata.candles_loaded
    3. len(full_candle_timestamps) + metadata.candles_loaded
    4. metadata.input_slice_first_timestamp exact unique match (legacy)
    5. metadata.input_slice_start_timestamp exact unique match (legacy)
    """
    explicit = continuous_metadata.get("input_slice_start_index")
    if explicit is not None:
        try:
            return (
                InputSliceStartResolution(
                    input_slice_start_index=int(explicit),
                    resolution_source="metadata_input_slice_start_index",
                    input_slice_first_timestamp=continuous_metadata.get("input_slice_first_timestamp"),
                ),
                None,
            )
        except (TypeError, ValueError):
            return None, "invalid_metadata_input_slice_start_index"

    candles_loaded = continuous_metadata.get("candles_loaded")
    total_count = continuous_metadata.get("candle_source_total_count")
    if total_count is not None and candles_loaded is not None:
        try:
            start_index = compute_input_slice_start_index(
                total_candle_count=int(total_count),
                slice_candle_count=int(candles_loaded),
            )
            return (
                InputSliceStartResolution(
                    input_slice_start_index=start_index,
                    resolution_source="metadata_computed_from_candles_loaded_and_total",
                    input_slice_first_timestamp=continuous_metadata.get("input_slice_first_timestamp"),
                ),
                None,
            )
        except (TypeError, ValueError):
            return None, "invalid_metadata_candle_counts"

    if candles_loaded is not None and full_candle_timestamps:
        try:
            start_index = compute_input_slice_start_index(
                total_candle_count=len(full_candle_timestamps),
                slice_candle_count=int(candles_loaded),
            )
            first_ts = full_candle_timestamps[start_index]
            return (
                InputSliceStartResolution(
                    input_slice_start_index=start_index,
                    resolution_source="legacy_computed_from_full_candle_file_and_candles_loaded",
                    input_slice_first_timestamp=first_ts.isoformat() if first_ts is not None else None,
                ),
                None,
            )
        except (TypeError, ValueError):
            return None, "invalid_legacy_candle_counts"

    for key, source in (
        ("input_slice_first_timestamp", "legacy_exact_slice_start_timestamp_match"),
        ("input_slice_start_timestamp", "legacy_exact_slice_start_timestamp_match"),
    ):
        ts_raw = continuous_metadata.get(key)
        if not ts_raw:
            continue
        try:
            target = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            return None, f"invalid_{key}"
        matched_index, match_error = exact_timestamp_match_index(full_candle_timestamps, target)
        if match_error:
            return None, match_error
        assert matched_index is not None
        matched_ts = full_candle_timestamps[matched_index]
        return (
            InputSliceStartResolution(
                input_slice_start_index=matched_index,
                resolution_source=source,
                input_slice_first_timestamp=matched_ts.isoformat() if matched_ts is not None else None,
            ),
            None,
        )

    return None, "input_slice_start_index_not_resolvable"


def resolve_absolute_candle_index(
    *,
    stored_local_candle_index: int | None,
    stored_slice_candle_index: int | None,
    stored_global_candle_index: int | None,
    input_slice_start_index: int,
    index_resolution_source: str,
) -> ResolvedCandleIndex | None:
    """
    Convert stored snapshot indices to an absolute feather index.

    Legacy continuous snapshots stored ``global_candle_index`` relative to the
  backtest input slice (not the full feather file). New snapshots store explicit
    ``slice_candle_index`` and ``absolute_candle_index``.
    """
    slice_index: int | None = None
    resolved_source = index_resolution_source

    if stored_slice_candle_index is not None:
        slice_index = int(stored_slice_candle_index)
        resolved_source = "snapshot_slice_candle_index"
    elif stored_global_candle_index is not None and index_resolution_source.startswith("legacy"):
        slice_index = int(stored_global_candle_index)
        resolved_source = "legacy_slice_relative_global_candle_index"
    elif stored_global_candle_index is not None and not index_resolution_source.startswith("legacy"):
        # New-format snapshots may already store absolute values in global_candle_index.
        absolute = int(stored_global_candle_index)
        return ResolvedCandleIndex(
            stored_local_candle_index=stored_local_candle_index,
            stored_slice_candle_index=stored_slice_candle_index,
            input_slice_start_index=int(input_slice_start_index),
            resolved_global_candle_index=absolute,
            index_offset=int(input_slice_start_index),
            index_resolution_source="snapshot_absolute_candle_index",
        )

    if slice_index is None:
        return None

    absolute = int(input_slice_start_index) + slice_index
    return ResolvedCandleIndex(
        stored_local_candle_index=stored_local_candle_index,
        stored_slice_candle_index=slice_index,
        input_slice_start_index=int(input_slice_start_index),
        resolved_global_candle_index=absolute,
        index_offset=int(input_slice_start_index),
        index_resolution_source=resolved_source,
    )


def index_diagnostics_for_candle(
    *,
    candles: Sequence[Any],
    stored_local_candle_index: int | None,
    stored_slice_candle_index: int | None,
    stored_global_candle_index: int | None,
    input_slice_start_index: int,
    slice_resolution_source: str,
    cycle3_fill_timestamp: str | None,
) -> dict[str, Any]:
    resolved = resolve_absolute_candle_index(
        stored_local_candle_index=stored_local_candle_index,
        stored_slice_candle_index=stored_slice_candle_index,
        stored_global_candle_index=stored_global_candle_index,
        input_slice_start_index=input_slice_start_index,
        index_resolution_source=slice_resolution_source,
    )
    diag: dict[str, Any] = {
        "stored_local_candle_index": stored_local_candle_index,
        "stored_global_candle_index": stored_global_candle_index,
        "stored_slice_candle_index": stored_slice_candle_index,
        "input_slice_start_index": input_slice_start_index,
        "index_resolution_source": slice_resolution_source,
        "resolved_global_candle_index": None,
        "index_offset": input_slice_start_index,
        "candle_timestamp_at_stored_index": None,
        "candle_timestamp_at_resolved_index": None,
        "cycle3_fill_timestamp": cycle3_fill_timestamp,
        "timestamp_matches_candle": None,
    }
    if resolved is None:
        return diag

    diag["stored_slice_candle_index"] = resolved.stored_slice_candle_index
    diag["resolved_global_candle_index"] = resolved.resolved_global_candle_index
    diag["index_resolution_source"] = resolved.index_resolution_source
    diag["index_offset"] = resolved.index_offset

    if (
        stored_global_candle_index is not None
        and 0 <= int(stored_global_candle_index) < len(candles)
    ):
        stored_ts = getattr(candles[int(stored_global_candle_index)], "timestamp", None)
        diag["candle_timestamp_at_stored_index"] = (
            stored_ts.isoformat() if stored_ts is not None else None
        )

    resolved_idx = resolved.resolved_global_candle_index
    if 0 <= resolved_idx < len(candles):
        resolved_ts = getattr(candles[resolved_idx], "timestamp", None)
        diag["candle_timestamp_at_resolved_index"] = (
            resolved_ts.isoformat() if resolved_ts is not None else None
        )
        if cycle3_fill_timestamp and resolved_ts is not None:
            try:
                fill_ts = datetime.fromisoformat(str(cycle3_fill_timestamp))
            except ValueError:
                fill_ts = None
            diag["timestamp_matches_candle"] = bool(fill_ts is not None and resolved_ts == fill_ts)

    return diag
