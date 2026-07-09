from __future__ import annotations

"""
Offline multi-trade audit for the long-only gap reduction scenario.

This module is backtest-only. It does not modify the live strategy or bot
behaviour. It operates purely on:

- a continuous *_continuous_results.json payload
- an associated 5m candle JSON file

and uses the existing single-trade `simulate_long_gap_reduction` helper to
simulate synthetic long-only gap-reduction steps for one or more trades.

Key design constraints:
- Gap-size semantics follow the single-trade model exactly:
  initial_gap_qty = max(long_qty_after_cycle3 - short_qty_after_cycle3, 0)
  planned_gap_reduce_qty_per_step = initial_gap_qty / 4
- Trigger definition is shared via `compute_trigger_price`:
  trigger_price(step) = reference_price * (0.99 ** step)
- A dedicated `--dry-run` mode validates inputs and snapshots but does not
  perform any simulations or write orders/events/summary CSVs.
"""

import argparse
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

from .candle_index_resolution import (
    InputSliceStartResolution,
    index_diagnostics_for_candle,
    resolve_absolute_candle_index,
    resolve_input_slice_start_index,
)
from .candle_loader import load_candles
from .long_gap_reduction import LongGapReductionConfig, simulate_long_gap_reduction
from .recovery_wait_activation import (
    RecoveryWaitEvaluation,
    evaluate_recovery_wait_activation,
    evaluation_to_dict,
    is_false_positive,
    original_exit_absolute_index,
)


LOGGER_NAME = "long_gap_reduction_multi_trade_audit"

DEFAULT_RECOVERY_START_PURPOSE = "CYCLE_3_SHORT_REDUCE"
ALLOWED_RECOVERY_START_PURPOSES = frozenset(
    {
        "CYCLE_3_SHORT_REDUCE",
        "CYCLE_4_LONG_ADD",
        "CYCLE_4_SHORT_REDUCE",
    }
)
CANDLE_INTERVAL_MINUTES = 5
JOINT_EXIT_DOCUMENTATION = (
    "joint_exit_timestamp equals the timestamp of the last LONG_REDUCE fill "
    "when gap_fully_closed is true after the fourth planned reduce step"
)


def normalize_recovery_start_purpose(value: str | None) -> str:
    purpose = str(value or DEFAULT_RECOVERY_START_PURPOSE).strip()
    if purpose not in ALLOWED_RECOVERY_START_PURPOSES:
        raise ValueError(
            f"unsupported recovery_start_purpose={purpose!r}; "
            f"allowed={sorted(ALLOWED_RECOVERY_START_PURPOSES)}"
        )
    return purpose


def _minutes_between_timestamps(left: object, right: object) -> float | None:
    start = _parse_iso_timestamp(left)
    end = _parse_iso_timestamp(right)
    if start is None or end is None:
        return None
    return (end - start).total_seconds() / 60.0


def _candles_between_indices(left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    return int(right) - int(left)


def _trade_blocks_fill_rows_for_purpose(
    blocks: List[Dict[str, Any]],
    purpose: str,
) -> List[Dict[str, Any]]:
    return [
        row
        for row in blocks
        if str(row.get("row_type") or "") == "fill"
        and str(row.get("purpose") or "") == purpose
    ]


def _purpose_reached_in_trade_blocks(
    *,
    base_dir: Path,
    symbol: str,
    trade_number: int,
    purpose: str,
) -> bool:
    trade_blocks_path = _find_trade_blocks_file_for_run(
        base_dir=base_dir,
        symbol=symbol,
        trade_number=trade_number,
    )
    if trade_blocks_path is None:
        return False
    payload = json.loads(trade_blocks_path.read_text(encoding="utf-8"))
    blocks = list(payload.get("trade_blocks") or payload.get("rows") or [])
    return bool(_trade_blocks_fill_rows_for_purpose(blocks, purpose))


@dataclass
class _Candle:
    timestamp: datetime | None
    open: float
    high: float
    low: float
    close: float


def _parse_iso_timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        # datetime.fromisoformat handles timezone suffixes for our purposes.
        return datetime.fromisoformat(str(value))
    except Exception:
        return None


def _load_candles_from_path(path: Path) -> Tuple[List[_Candle], Dict[str, Any]]:
    """
    Load normalized candle objects from a JSON file.

    The loader is intentionally tolerant and supports two common shapes:

    1) A plain list of candle dicts:
       [
         {"timestamp": "...", "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.0},
         ...
       ]

    2) A dict wrapper with metadata:
       {
         "symbol": "APTUSDT",
         "timeframe": "5m",
         "candles": [...]
       }
    """
    meta: Dict[str, Any] = {}
    raw_candles: List[Dict[str, Any]] = []

    suffix = path.suffix.lower()
    if suffix in {".json"}:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and "candles" in payload:
            raw_candles = list(payload.get("candles") or [])
            meta = {
                "symbol": payload.get("symbol"),
                "timeframe": payload.get("timeframe"),
            }
        else:
            raw_candles = list(payload or [])
    else:
        # Delegate to the shared candle_loader for CSV/Feather inputs.
        raw_candles = load_candles(path, limit=None)

    candles: List[_Candle] = []
    for row in raw_candles or []:
        ts = _parse_iso_timestamp((row or {}).get("timestamp"))
        candles.append(
            _Candle(
                timestamp=ts,
                open=float((row or {}).get("open")),
                high=float((row or {}).get("high")),
                low=float((row or {}).get("low")),
                close=float((row or {}).get("close")),
            )
        )

    if candles:
        meta.setdefault("first_timestamp", candles[0].timestamp.isoformat() if candles[0].timestamp else None)
        meta.setdefault("last_timestamp", candles[-1].timestamp.isoformat() if candles[-1].timestamp else None)
        meta.setdefault("candle_count", len(candles))

    return candles, meta


def _ensure_logger(log_path: Path) -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    # Avoid attaching multiple handlers when run repeatedly in tests.
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "_lg_path", None) == str(log_path) for h in logger.handlers):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_path, encoding="utf-8")
        handler._lg_path = str(log_path)  # type: ignore[attr-defined]
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    return logger


def _write_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    import csv

    rows = list(rows)
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _load_continuous_results(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("continuous_results JSON must be an object")
    if "runs" not in payload or not isinstance(payload.get("runs"), list):
        raise ValueError("continuous_results JSON must contain a 'runs' list")
    return payload


def _select_runs(
    runs: List[Dict[str, Any]],
    trade_numbers: List[int] | None,
) -> List[Dict[str, Any]]:
    if trade_numbers:
        wanted = {int(n) for n in trade_numbers}
        return [run for run in runs if int(run.get("trade_number") or 0) in wanted]
    return list(runs)


def _find_trade_blocks_file_for_run(
    *,
    base_dir: Path,
    symbol: str,
    trade_number: int,
) -> Path | None:
    """
    Resolve the trade-blocks JSON file for a given symbol/trade_number.

    The standard naming pattern from ``trade_block_export`` is:

        {SYMBOL}_long_continuous_trade_{trade:04d}_{fill_model}_*_trade_blocks.json
    """
    symbol_upper = symbol.upper()
    pattern = f"{symbol_upper}_long_continuous_trade_{trade_number:04d}_*_trade_blocks.json"
    candidates = list(base_dir.glob(pattern))
    if not candidates:
        return None
    return sorted(candidates)[0]


def _init_recovery_diag(trade_number: int, recovery_start_purpose: str) -> Dict[str, Any]:
    return {
        "trade_number": trade_number,
        "recovery_trigger_purpose": recovery_start_purpose,
        "snapshot_source": None,
        "trigger_purpose_found": False,
        "cycle3_purpose_found": recovery_start_purpose == DEFAULT_RECOVERY_START_PURPOSE,
        "recovery_fill_timestamp": None,
        "cycle3_fill_timestamp": None,
        "recovery_local_candle_index": None,
        "cycle3_local_candle_index": None,
        "recovery_global_candle_index": None,
        "cycle3_global_candle_index": None,
        "stored_local_candle_index": None,
        "stored_global_candle_index": None,
        "stored_slice_candle_index": None,
        "input_slice_start_index": None,
        "resolved_global_candle_index": None,
        "candle_timestamp_at_stored_index": None,
        "candle_timestamp_at_resolved_index": None,
        "index_offset": None,
        "index_resolution_source": None,
        "timestamp_matches_candle": None,
        "snapshot_validation_errors": [],
        "reason": None,
    }


def _apply_index_resolution_to_diag(
    diag: Dict[str, Any],
    *,
    candles: List[_Candle],
    stored_local_candle_index: int | None,
    stored_slice_candle_index: int | None,
    stored_global_candle_index: int | None,
    input_slice_start_index: int,
    slice_resolution_source: str,
    cycle3_fill_timestamp: str | None,
    legacy_slice_relative_global: bool,
) -> Dict[str, Any]:
    index_source = (
        "legacy_slice_relative_global_candle_index"
        if legacy_slice_relative_global
        else slice_resolution_source
    )
    index_diag = index_diagnostics_for_candle(
        candles=candles,
        stored_local_candle_index=stored_local_candle_index,
        stored_slice_candle_index=stored_slice_candle_index,
        stored_global_candle_index=stored_global_candle_index,
        input_slice_start_index=input_slice_start_index,
        slice_resolution_source=index_source,
        cycle3_fill_timestamp=cycle3_fill_timestamp,
    )
    diag.update(index_diag)
    diag["recovery_local_candle_index"] = stored_local_candle_index
    diag["cycle3_local_candle_index"] = stored_local_candle_index
    diag["recovery_global_candle_index"] = stored_global_candle_index
    diag["cycle3_global_candle_index"] = stored_global_candle_index
    return diag


def _build_recovery_snapshot(
    *,
    trade_number: int,
    recovery_start_purpose: str,
    resolved_absolute: int,
    local_idx_int: int | None,
    slice_idx_int: int | None,
    fill_timestamp: str | None,
    fill_price: float,
    long_qty_after: float,
    short_qty_after: float,
    long_avg_after: float,
    short_avg_after: float,
    realized_pnl_at_start: float,
    active_cycle: int | None,
) -> Dict[str, Any]:
    snapshot: Dict[str, Any] = {
        "trade_number": trade_number,
        "recovery_start_purpose": recovery_start_purpose,
        "recovery_candle_index": resolved_absolute,
        "recovery_local_candle_index": local_idx_int,
        "recovery_slice_candle_index": slice_idx_int,
        "recovery_fill_timestamp": fill_timestamp,
        "recovery_fill_price": fill_price,
        "long_qty_at_recovery_start": long_qty_after,
        "short_qty_at_recovery_start": short_qty_after,
        "long_avg_at_recovery_start": long_avg_after,
        "short_avg_at_recovery_start": short_avg_after,
        "realized_pnl_at_recovery_start": realized_pnl_at_start,
        "active_cycle": active_cycle,
    }
    if recovery_start_purpose == DEFAULT_RECOVERY_START_PURPOSE:
        snapshot.update(
            {
                "cycle3_candle_index": resolved_absolute,
                "cycle3_local_candle_index": local_idx_int,
                "cycle3_slice_candle_index": slice_idx_int,
                "cycle3_fill_price": fill_price,
                "long_qty_after_cycle3": long_qty_after,
                "short_qty_after_cycle3": short_qty_after,
                "long_avg_after_cycle3": long_avg_after,
                "short_avg_after_cycle3": short_avg_after,
                "realized_pnl_at_cycle3": realized_pnl_at_start,
            }
        )
    return snapshot


def _snapshot_recovery_index(snapshot: Dict[str, Any]) -> int:
    return int(snapshot.get("recovery_candle_index") or snapshot["cycle3_candle_index"])


def _snapshot_recovery_fill_price(snapshot: Dict[str, Any]) -> float:
    return float(snapshot.get("recovery_fill_price") or snapshot["cycle3_fill_price"])


def _snapshot_long_qty(snapshot: Dict[str, Any]) -> float:
    return float(snapshot.get("long_qty_at_recovery_start") or snapshot["long_qty_after_cycle3"])


def _snapshot_short_qty(snapshot: Dict[str, Any]) -> float:
    return float(snapshot.get("short_qty_at_recovery_start") or snapshot["short_qty_after_cycle3"])


def _snapshot_long_avg(snapshot: Dict[str, Any]) -> float:
    return float(snapshot.get("long_avg_at_recovery_start") or snapshot["long_avg_after_cycle3"])


def _snapshot_short_avg(snapshot: Dict[str, Any]) -> float:
    return float(snapshot.get("short_avg_at_recovery_start") or snapshot["short_avg_after_cycle3"])


def _snapshot_realized_pnl(snapshot: Dict[str, Any]) -> float:
    return float(
        snapshot.get("realized_pnl_at_recovery_start")
        or snapshot.get("realized_pnl_at_cycle3")
        or 0.0
    )


def _compute_timing_and_original_comparison(
    *,
    run: Dict[str, Any],
    snapshot: Dict[str, Any],
    events: List[Dict[str, Any]],
    lg_summary: Dict[str, Any],
    recovery_start_purpose: str,
    recovery_candle_index: int,
) -> Dict[str, Any]:
    trade_start_ts = run.get("start_time")
    recovery_start_ts = snapshot.get("recovery_fill_timestamp")
    reduce_events = [event for event in events if event.get("event_type") == "LONG_REDUCE"]
    first_reduce = reduce_events[0] if reduce_events else None
    last_reduce = reduce_events[-1] if reduce_events else None
    gap_fully_closed = bool(lg_summary.get("gap_fully_closed"))
    joint_exit_ts = last_reduce.get("timestamp") if gap_fully_closed and last_reduce else None

    start_event = next((event for event in events if event.get("event_type") == "START"), None)
    end_event = next((event for event in reversed(events) if event.get("event_type") == "END"), None)
    total_pnl_at_recovery = (
        float(start_event.get("total_trade_pnl"))
        if start_event and start_event.get("total_trade_pnl") is not None
        else _snapshot_realized_pnl(snapshot)
    )
    total_pnl_at_joint_exit = (
        float(last_reduce.get("total_trade_pnl"))
        if gap_fully_closed and last_reduce and last_reduce.get("total_trade_pnl") is not None
        else float(end_event.get("total_trade_pnl"))
        if end_event and end_event.get("total_trade_pnl") is not None
        else None
    )

    original_closed = str(run.get("final_status") or "").lower() == "closed"
    original_exit_ts = run.get("end_time") if original_closed else None
    original_end_pnl = run.get("realized_pnl")
    recovery_exit_ts = joint_exit_ts
    recovery_end_pnl = total_pnl_at_joint_exit

    return {
        "trade_start_timestamp": trade_start_ts,
        "recovery_trigger_purpose": recovery_start_purpose,
        "recovery_start_timestamp": recovery_start_ts,
        "recovery_start_absolute_candle_index": recovery_candle_index,
        "first_gap_reduce_timestamp": first_reduce.get("timestamp") if first_reduce else None,
        "last_gap_reduce_timestamp": last_reduce.get("timestamp") if last_reduce else None,
        "joint_exit_timestamp": joint_exit_ts,
        "joint_exit_semantics": JOINT_EXIT_DOCUMENTATION if gap_fully_closed else None,
        "candles_trade_start_to_recovery": _candles_between_indices(
            int(run.get("start_index") or 0),
            snapshot.get("recovery_local_candle_index"),
        ),
        "minutes_trade_start_to_recovery": _minutes_between_timestamps(
            trade_start_ts,
            recovery_start_ts,
        ),
        "candles_recovery_to_joint_exit": _candles_between_indices(
            0,
            int(last_reduce.get("candle_index"))
            if gap_fully_closed and last_reduce and last_reduce.get("candle_index") is not None
            else None,
        ),
        "minutes_recovery_to_joint_exit": _minutes_between_timestamps(
            recovery_start_ts,
            joint_exit_ts,
        ),
        "candles_trade_start_to_joint_exit": _candles_between_indices(
            int(run.get("start_index") or 0),
            int(last_reduce.get("absolute_candle_index"))
            if gap_fully_closed and last_reduce and last_reduce.get("absolute_candle_index") is not None
            else None,
        ),
        "minutes_trade_start_to_joint_exit": _minutes_between_timestamps(
            trade_start_ts,
            joint_exit_ts,
        ),
        "total_trade_pnl_at_recovery_start": total_pnl_at_recovery,
        "total_trade_pnl_at_joint_exit": total_pnl_at_joint_exit,
        "original_status": "closed" if original_closed else "open",
        "original_exit_timestamp": original_exit_ts,
        "original_end_pnl": original_end_pnl,
        "recovery_variant_activated": True,
        "recovery_exit_timestamp": recovery_exit_ts,
        "recovery_end_pnl": recovery_end_pnl,
        "minutes_original_exit_to_recovery_exit": _minutes_between_timestamps(
            original_exit_ts,
            recovery_exit_ts,
        ),
        "gap_fully_closed": gap_fully_closed,
    }


def _resolved_index_from_embedded_snapshot(
    embedded_snapshot: Dict[str, Any],
    *,
    input_slice_start_index: int,
    legacy_slice_relative_global: bool,
) -> tuple[int | None, int | None, int | None]:
    local_idx = embedded_snapshot.get("local_candle_index")
    slice_idx = embedded_snapshot.get("slice_candle_index")
    stored_global = embedded_snapshot.get("global_candle_index")
    absolute_idx = embedded_snapshot.get("absolute_candle_index")

    try:
        local_idx_int = int(local_idx) if local_idx is not None else None
    except (TypeError, ValueError):
        local_idx_int = None

    if absolute_idx is not None:
        try:
            resolved_absolute = int(absolute_idx)
        except (TypeError, ValueError):
            resolved_absolute = None
        try:
            slice_idx_int = int(slice_idx) if slice_idx is not None else None
        except (TypeError, ValueError):
            slice_idx_int = None
        return local_idx_int, slice_idx_int, resolved_absolute

    resolution = resolve_absolute_candle_index(
        stored_local_candle_index=local_idx_int,
        stored_slice_candle_index=int(slice_idx) if slice_idx is not None else None,
        stored_global_candle_index=int(stored_global) if stored_global is not None else None,
        input_slice_start_index=input_slice_start_index,
        index_resolution_source=(
            "legacy_slice_relative_global_candle_index"
            if legacy_slice_relative_global
            else "snapshot_slice_candle_index"
        ),
    )
    if resolution is None:
        return local_idx_int, None, None
    return local_idx_int, resolution.stored_slice_candle_index, resolution.resolved_global_candle_index


def _extract_recovery_snapshot_for_run(
    run: Dict[str, Any],
    *,
    recovery_start_purpose: str,
    base_dir: Path,
    symbol: str,
    candles: List[_Candle],
    input_slice_start_index: int,
    slice_resolution_source: str,
    legacy_slice_relative_global: bool,
) -> Tuple[Dict[str, Any] | None, Dict[str, Any]]:
    trade_number = int(run.get("trade_number") or 0)
    diag = _init_recovery_diag(trade_number, recovery_start_purpose)

    def _finalize_index_diag(
        *,
        stored_local: int | None,
        stored_slice: int | None,
        stored_global: int | None,
        fill_timestamp: str | None,
    ) -> None:
        _apply_index_resolution_to_diag(
            diag,
            candles=candles,
            stored_local_candle_index=stored_local,
            stored_slice_candle_index=stored_slice,
            stored_global_candle_index=stored_global,
            input_slice_start_index=input_slice_start_index,
            slice_resolution_source=slice_resolution_source,
            cycle3_fill_timestamp=fill_timestamp,
            legacy_slice_relative_global=legacy_slice_relative_global,
        )
        diag["recovery_fill_timestamp"] = fill_timestamp
        diag["cycle3_fill_timestamp"] = fill_timestamp
        diag["trigger_purpose_found"] = True
        diag["cycle3_purpose_found"] = recovery_start_purpose == DEFAULT_RECOVERY_START_PURPOSE

    if recovery_start_purpose == DEFAULT_RECOVERY_START_PURPOSE:
        embedded_snapshot = run.get("cycle3_snapshot")
        if isinstance(embedded_snapshot, dict):
            diag["snapshot_source"] = "continuous_embedded"
            purpose = str(embedded_snapshot.get("purpose") or "")
            if purpose != recovery_start_purpose:
                diag["snapshot_validation_errors"].append("embedded_snapshot_purpose_mismatch")
            else:
                ts_str = str(embedded_snapshot.get("timestamp") or "")
                local_idx_int, slice_idx_int, resolved_absolute = _resolved_index_from_embedded_snapshot(
                    embedded_snapshot,
                    input_slice_start_index=input_slice_start_index,
                    legacy_slice_relative_global=legacy_slice_relative_global,
                )
                stored_global = embedded_snapshot.get("global_candle_index")
                try:
                    stored_global_int = int(stored_global) if stored_global is not None else None
                except (TypeError, ValueError):
                    stored_global_int = None
                _finalize_index_diag(
                    stored_local=local_idx_int,
                    stored_slice=slice_idx_int if slice_idx_int is not None else stored_global_int,
                    stored_global=stored_global_int,
                    fill_timestamp=ts_str or None,
                )
                if resolved_absolute is None:
                    diag["snapshot_validation_errors"].append("recovery_index_not_resolvable")
                elif not (0 <= resolved_absolute < len(candles)):
                    diag["snapshot_validation_errors"].append("recovery_index_out_of_range")
                elif diag.get("timestamp_matches_candle") is False:
                    diag["snapshot_validation_errors"].append("timestamp_mismatch_embedded_snapshot")

                mandatory_keys = (
                    "fill_price",
                    "filled_qty",
                    "long_qty_after",
                    "short_qty_after",
                    "long_avg_after",
                    "short_avg_after",
                    "cumulative_realized_pnl_net",
                )
                missing = [key for key in mandatory_keys if embedded_snapshot.get(key) is None]
                if missing:
                    diag["snapshot_validation_errors"].append(
                        f"missing_fields:{','.join(sorted(missing))}"
                    )
                if not diag["snapshot_validation_errors"]:
                    active_cycle = embedded_snapshot.get("active_cycle")
                    if active_cycle is None:
                        active_cycle = embedded_snapshot.get("cycle_index")
                    try:
                        active_cycle_int = int(active_cycle) if active_cycle is not None else None
                    except (TypeError, ValueError):
                        active_cycle_int = None
                    return (
                        _build_recovery_snapshot(
                            trade_number=trade_number,
                            recovery_start_purpose=recovery_start_purpose,
                            resolved_absolute=int(resolved_absolute),
                            local_idx_int=local_idx_int,
                            slice_idx_int=slice_idx_int,
                            fill_timestamp=ts_str or None,
                            fill_price=float(embedded_snapshot["fill_price"]),
                            long_qty_after=float(embedded_snapshot["long_qty_after"]),
                            short_qty_after=float(embedded_snapshot["short_qty_after"]),
                            long_avg_after=float(embedded_snapshot["long_avg_after"]),
                            short_avg_after=float(embedded_snapshot["short_avg_after"]),
                            realized_pnl_at_start=float(
                                embedded_snapshot.get("cumulative_realized_pnl_net") or 0.0
                            ),
                            active_cycle=active_cycle_int,
                        ),
                        diag,
                    )
            if not diag.get("reason"):
                diag["reason"] = "embedded_snapshot_failed_validation"

    trade_blocks_path = _find_trade_blocks_file_for_run(
        base_dir=base_dir,
        symbol=symbol,
        trade_number=trade_number,
    )
    if trade_blocks_path is None:
        diag["reason"] = diag.get("reason") or "trade_blocks_file_not_found"
        diag["snapshot_source"] = diag.get("snapshot_source") or "none"
        return None, diag

    payload = json.loads(trade_blocks_path.read_text(encoding="utf-8"))
    blocks = list(payload.get("trade_blocks") or payload.get("rows") or [])
    metadata = dict(payload.get("metadata") or {})
    trade_start_index = int(metadata.get("start_index") or run.get("start_index") or 0)
    fill_rows = _trade_blocks_fill_rows_for_purpose(blocks, recovery_start_purpose)
    diag["snapshot_source"] = "trade_blocks"
    diag["trigger_purpose_found"] = bool(fill_rows)
    diag["cycle3_purpose_found"] = recovery_start_purpose == DEFAULT_RECOVERY_START_PURPOSE and bool(fill_rows)

    if not fill_rows:
        diag["reason"] = diag.get("reason") or "recovery_fill_not_found"
        return None, diag

    fill_row = fill_rows[-1]
    local_idx = int(fill_row.get("candle_index") or fill_row.get("local_candle_index") or 0)
    stored_slice = fill_row.get("slice_candle_index")
    if stored_slice is None:
        stored_slice = trade_start_index + local_idx
    try:
        stored_slice_int = int(stored_slice)
    except (TypeError, ValueError):
        stored_slice_int = trade_start_index + local_idx

    ts_str = str(fill_row.get("timestamp") or "")
    _finalize_index_diag(
        stored_local=local_idx,
        stored_slice=stored_slice_int,
        stored_global=stored_slice_int,
        fill_timestamp=ts_str or None,
    )
    resolved_absolute = diag.get("resolved_global_candle_index")

    long_qty_after = fill_row.get("long_qty_after")
    short_qty_after = fill_row.get("short_qty_after")
    long_avg_after = fill_row.get("long_avg_after")
    short_avg_after = fill_row.get("short_avg_after")
    fill_price = fill_row.get("fill_price")
    cumulative_pnl = fill_row.get("cumulative_realized_pnl_net")
    if cumulative_pnl is None:
        cumulative_pnl = fill_row.get("cumulative_pnl")
    active_cycle = fill_row.get("cycle_index")

    missing_fields: list[str] = []
    for key, value in (
        ("long_qty_after", long_qty_after),
        ("short_qty_after", short_qty_after),
        ("long_avg_after", long_avg_after),
        ("short_avg_after", short_avg_after),
        ("fill_price", fill_price),
        ("cumulative_pnl", cumulative_pnl),
    ):
        if value is None:
            missing_fields.append(key)

    if missing_fields:
        diag["snapshot_validation_errors"].append(
            f"missing_fields:{','.join(sorted(missing_fields))}"
        )
    if diag.get("timestamp_matches_candle") is False:
        diag["snapshot_validation_errors"].append("timestamp_mismatch_trade_blocks")
    if resolved_absolute is None:
        diag["snapshot_validation_errors"].append("recovery_index_not_resolvable")
    elif not (0 <= int(resolved_absolute) < len(candles)):
        diag["snapshot_validation_errors"].append("recovery_index_out_of_range")

    if diag["snapshot_validation_errors"]:
        diag["reason"] = "trade_blocks_snapshot_failed_validation"
        return None, diag

    try:
        active_cycle_int = int(active_cycle) if active_cycle is not None else None
    except (TypeError, ValueError):
        active_cycle_int = None

    return (
        _build_recovery_snapshot(
            trade_number=trade_number,
            recovery_start_purpose=recovery_start_purpose,
            resolved_absolute=int(resolved_absolute),
            local_idx_int=local_idx,
            slice_idx_int=stored_slice_int,
            fill_timestamp=ts_str or None,
            fill_price=float(fill_price),
            long_qty_after=float(long_qty_after),
            short_qty_after=float(short_qty_after),
            long_avg_after=float(long_avg_after),
            short_avg_after=float(short_avg_after),
            realized_pnl_at_start=float(cumulative_pnl),
            active_cycle=active_cycle_int,
        ),
        diag,
    )


def _extract_cycle3_snapshot_for_run(
    run: Dict[str, Any],
    *,
    base_dir: Path,
    symbol: str,
    candles: List[_Candle],
    input_slice_start_index: int,
    slice_resolution_source: str,
    legacy_slice_relative_global: bool,
) -> Tuple[Dict[str, Any] | None, Dict[str, Any]]:
    return _extract_recovery_snapshot_for_run(
        run,
        recovery_start_purpose=DEFAULT_RECOVERY_START_PURPOSE,
        base_dir=base_dir,
        symbol=symbol,
        candles=candles,
        input_slice_start_index=input_slice_start_index,
        slice_resolution_source=slice_resolution_source,
        legacy_slice_relative_global=legacy_slice_relative_global,
    )


def _load_variant_summary_rows(summary_json_path: Path) -> Dict[int, Dict[str, Any]]:
    if not summary_json_path.exists():
        return {}
    payload = json.loads(summary_json_path.read_text(encoding="utf-8"))
    rows = payload.get("trades") or []
    return {int(row.get("trade_number") or 0): row for row in rows}


def _build_wait_mode_summary_row(
    *,
    run_id: str,
    run: Dict[str, Any],
    recovery_start_purpose: str,
    reference_snapshot: Dict[str, Any] | None,
    wait_eval: RecoveryWaitEvaluation | None,
    events: List[Dict[str, Any]] | None,
    lg_summary: Dict[str, Any] | None,
    recovery_candle_index: int | None,
) -> Dict[str, Any]:
    trade_number = int(run.get("trade_number") or 0)
    original_closed = str(run.get("final_status") or "").lower() == "closed"
    original_pnl = run.get("realized_pnl")
    row: Dict[str, Any] = {
        "run_id": run_id,
        "trade_number": trade_number,
        "recovery_start_purpose": recovery_start_purpose,
        "original_status": "closed" if original_closed else "open",
        "original_pnl": original_pnl,
        "original_end_pnl": original_pnl,
        "trade_start_timestamp": run.get("start_time"),
        "c4_long_add_trigger_reached": reference_snapshot is not None,
        "wait_end_reached": bool(
            wait_eval
            and wait_eval.recovery_activation_absolute_candle_index is not None
            and (
                wait_eval.recovery_wait_candles == 0
                or wait_eval.non_activation_reason != "series_ended_before_activation"
            )
        ),
        "trade_open_at_wait_end": bool(
            wait_eval
            and wait_eval.recovery_activated
            or (
                wait_eval
                and not wait_eval.original_closed_before_recovery
                and wait_eval.non_activation_reason
                in {None, "no_long_short_gap_at_activation"}
            )
        ),
        "false_positive": False,
        "recovery_final_pnl": original_pnl if original_closed else None,
    }
    if wait_eval is not None:
        row.update(evaluation_to_dict(wait_eval))
        row["recovery_variant_activated"] = wait_eval.recovery_activated
        row["recovery_activated"] = wait_eval.recovery_activated
        row["false_positive"] = is_false_positive(
            original_status="closed" if original_closed else "open",
            original_pnl=float(original_pnl) if original_pnl is not None else None,
            recovery_activated=wait_eval.recovery_activated,
            original_closed_before_recovery=wait_eval.original_closed_before_recovery,
        )
    elif reference_snapshot is None:
        row.update(
            {
                "recovery_reference_purpose": recovery_start_purpose,
                "recovery_wait_candles": 0,
                "recovery_wait_minutes": 0,
                "recovery_activated": False,
                "recovery_variant_activated": False,
                "non_activation_reason": "reference_trigger_not_reached",
                "activation_reason": None,
            }
        )
    if wait_eval and wait_eval.recovery_activated and events and lg_summary:
        reduce_events = [event for event in events if event.get("event_type") == "LONG_REDUCE"]
        first_reduce = reduce_events[0] if reduce_events else None
        last_reduce = reduce_events[-1] if reduce_events else None
        gap_fully_closed = bool(lg_summary.get("gap_fully_closed"))
        joint_exit_ts = last_reduce.get("timestamp") if gap_fully_closed and last_reduce else None
        end_event = next((event for event in reversed(events) if event.get("event_type") == "END"), None)
        recovery_final_pnl = (
            float(last_reduce.get("total_trade_pnl"))
            if gap_fully_closed and last_reduce and last_reduce.get("total_trade_pnl") is not None
            else float(end_event.get("total_trade_pnl"))
            if end_event and end_event.get("total_trade_pnl") is not None
            else None
        )
        row.update(
            {
                "recovery_candle_index": recovery_candle_index,
                "recovery_start_timestamp": wait_eval.recovery_activation_timestamp,
                "recovery_start_absolute_candle_index": recovery_candle_index,
                "first_gap_reduce_timestamp": first_reduce.get("timestamp") if first_reduce else None,
                "last_gap_reduce_timestamp": last_reduce.get("timestamp") if last_reduce else None,
                "joint_exit_timestamp": joint_exit_ts,
                "recovery_exit_timestamp": joint_exit_ts,
                "recovery_end_pnl": recovery_final_pnl,
                "recovery_final_pnl": recovery_final_pnl,
                "initial_long_qty": lg_summary.get("initial_long_qty"),
                "initial_short_qty": lg_summary.get("initial_short_qty"),
                "initial_gap_qty": lg_summary.get("initial_gap_qty"),
                "planned_gap_reduce_qty_per_step": lg_summary.get("planned_gap_reduce_qty_per_step"),
                "total_reduced_qty": lg_summary.get("total_reduced_qty"),
                "total_gap_reduction_net_pnl": lg_summary.get("total_gap_reduction_net_pnl"),
                "final_long_qty": lg_summary.get("final_long_qty"),
                "final_short_qty": lg_summary.get("final_short_qty"),
                "remaining_gap_qty": lg_summary.get("remaining_gap_qty"),
                "gap_fully_closed": lg_summary.get("gap_fully_closed"),
                "minutes_trade_start_to_joint_exit": _minutes_between_timestamps(
                    run.get("start_time"),
                    joint_exit_ts,
                ),
            }
        )
    elif wait_eval and not wait_eval.recovery_activated:
        row["recovery_variant_activated"] = False
        row["recovery_activated"] = False
        row["recovery_exit_timestamp"] = None
        row["recovery_end_pnl"] = original_pnl if original_closed else None
        row["recovery_final_pnl"] = original_pnl if original_closed else None
    return row


def write_c4la_wait_comparison_table(
    *,
    continuous_results_path: Path,
    wait_summary_json_by_candles: Dict[int, Path],
    output_path: Path,
) -> Path:
    continuous = _load_continuous_results(continuous_results_path)
    runs = {int(run.get("trade_number") or 0): run for run in continuous.get("runs") or []}
    wait_rows_by_candles: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for wait_candles, summary_path in sorted(wait_summary_json_by_candles.items()):
        wait_rows_by_candles[wait_candles] = _load_variant_summary_rows(summary_path)

    comparison_rows: List[Dict[str, Any]] = []
    aggregate_rows: List[Dict[str, Any]] = []
    for wait_candles in sorted(wait_summary_json_by_candles):
        variant_rows = wait_rows_by_candles[wait_candles]
        activated = [row for row in variant_rows.values() if row.get("recovery_activated")]
        false_positives = [row for row in variant_rows.values() if row.get("false_positive")]
        blocker = variant_rows.get(12, {})
        aggregate_rows.append(
            {
                "wait_candles": wait_candles,
                "activated_trade_count": len(activated),
                "false_positive_count": len(false_positives),
                "blocker_0012_detected": 12 in variant_rows and bool(
                    variant_rows[12].get("c4_long_add_trigger_reached")
                ),
                "blocker_0012_activation_timestamp": blocker.get("recovery_activation_timestamp"),
                "blocker_0012_exit_timestamp": blocker.get("recovery_exit_timestamp")
                or blocker.get("joint_exit_timestamp"),
                "blocker_0012_final_pnl": blocker.get("recovery_final_pnl")
                or blocker.get("recovery_end_pnl"),
            }
        )

    for trade_number in sorted(runs):
        run = runs[trade_number]
        original_closed = str(run.get("final_status") or "").lower() == "closed"
        row: Dict[str, Any] = {
            "trade_number": trade_number,
            "original_status": "closed" if original_closed else "open",
            "original_pnl": run.get("realized_pnl"),
            "original_exit_timestamp": run.get("end_time") if original_closed else None,
        }
        for wait_candles in sorted(wait_summary_json_by_candles):
            variant = wait_rows_by_candles[wait_candles].get(trade_number, {})
            prefix = f"wait_{wait_candles}"
            row[f"{prefix}_activated"] = bool(variant.get("recovery_activated"))
            row[f"{prefix}_exit_timestamp"] = (
                variant.get("recovery_exit_timestamp")
                or variant.get("joint_exit_timestamp")
                or (run.get("end_time") if original_closed else None)
            )
            row[f"{prefix}_pnl"] = (
                variant.get("recovery_final_pnl")
                if variant.get("recovery_activated")
                else variant.get("recovery_final_pnl")
                if variant
                else run.get("realized_pnl")
            )
            row[f"{prefix}_false_positive"] = bool(variant.get("false_positive"))
        comparison_rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(output_path, comparison_rows)
    aggregate_path = output_path.with_name("long_gap_reduction_c4la_wait_aggregate_summary.csv")
    _write_csv(aggregate_path, aggregate_rows)
    output_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "comparison_rows": comparison_rows,
                "aggregate_rows": aggregate_rows,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return output_path


def write_recovery_variant_comparison_table(
    *,
    continuous_results_path: Path,
    c3_summary_json: Path,
    c4_long_add_summary_json: Path,
    c4_short_reduce_summary_json: Path,
    output_path: Path,
    base_dir: Path,
    symbol: str,
) -> Path:
    continuous = _load_continuous_results(continuous_results_path)
    runs = {int(run.get("trade_number") or 0): run for run in continuous.get("runs") or []}
    c3_rows = _load_variant_summary_rows(c3_summary_json)
    c4_long_rows = _load_variant_summary_rows(c4_long_add_summary_json)
    c4_short_rows = _load_variant_summary_rows(c4_short_reduce_summary_json)

    comparison_rows: List[Dict[str, Any]] = []
    for trade_number in sorted(runs):
        run = runs[trade_number]
        original_closed = str(run.get("final_status") or "").lower() == "closed"
        c3 = c3_rows.get(trade_number, {})
        c4_long = c4_long_rows.get(trade_number, {})
        c4_short = c4_short_rows.get(trade_number, {})
        comparison_rows.append(
            {
                "trade_number": trade_number,
                "original_status": "closed" if original_closed else "open",
                "original_pnl": run.get("realized_pnl"),
                "original_exit_timestamp": run.get("end_time") if original_closed else None,
                "c3_recovery_pnl": c3.get("recovery_end_pnl"),
                "c4_long_add_trigger_reached": _purpose_reached_in_trade_blocks(
                    base_dir=base_dir,
                    symbol=symbol,
                    trade_number=trade_number,
                    purpose="CYCLE_4_LONG_ADD",
                ),
                "c4_long_add_activated": trade_number in c4_long_rows,
                "c4_long_add_pnl": c4_long.get("recovery_end_pnl"),
                "c4_short_reduce_trigger_reached": _purpose_reached_in_trade_blocks(
                    base_dir=base_dir,
                    symbol=symbol,
                    trade_number=trade_number,
                    purpose="CYCLE_4_SHORT_REDUCE",
                ),
                "c4_short_reduce_activated": trade_number in c4_short_rows,
                "c4_short_reduce_pnl": c4_short.get("recovery_end_pnl"),
                "original_exit": run.get("end_time") if original_closed else None,
                "c4_long_add_recovery_start": c4_long.get("recovery_start_timestamp"),
                "c4_long_add_exit": c4_long.get("recovery_exit_timestamp"),
                "c4_short_reduce_recovery_start": c4_short.get("recovery_start_timestamp"),
                "c4_short_reduce_exit": c4_short.get("recovery_exit_timestamp"),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(output_path, comparison_rows)
    timing_path = output_path.with_name("long_gap_reduction_variant_timing_comparison.csv")
    timing_rows = []
    for trade_number in sorted(runs):
        run = runs[trade_number]
        original_closed = str(run.get("final_status") or "").lower() == "closed"
        c4_long = c4_long_rows.get(trade_number, {})
        c4_short = c4_short_rows.get(trade_number, {})
        timing_rows.append(
            {
                "trade_number": trade_number,
                "original_exit": run.get("end_time") if original_closed else None,
                "c4_long_add_recovery_start": c4_long.get("recovery_start_timestamp"),
                "c4_long_add_exit": c4_long.get("recovery_exit_timestamp"),
                "c4_short_reduce_recovery_start": c4_short.get("recovery_start_timestamp"),
                "c4_short_reduce_exit": c4_short.get("recovery_exit_timestamp"),
            }
        )
    _write_csv(timing_path, timing_rows)
    comparison_json = {
        "comparison_rows": comparison_rows,
        "timing_rows": timing_rows,
    }
    output_path.with_suffix(".json").write_text(
        json.dumps(comparison_json, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return output_path


def run_long_gap_reduction_multi_trade_audit(
    *,
    input_results: Path,
    input_candles: Path,
    output_dir: Path,
    trade_numbers: List[int] | None = None,
    all_eligible_trades: bool = False,
    dry_run: bool = False,
    recovery_start_purpose: str = DEFAULT_RECOVERY_START_PURPOSE,
    recovery_wait_candles: int = 0,
) -> Dict[str, Any]:
    """
    High-level entry point for the multi-trade long-gap-reduction audit.

    Returns a dict with:
    - "run_id"
    - "preflight_path"
    - (optionally) CSV/JSON output paths for orders/events/summary when not in dry-run mode.
    """
    recovery_start_purpose = normalize_recovery_start_purpose(recovery_start_purpose)
    recovery_wait_candles = max(0, int(recovery_wait_candles))
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now(timezone.utc).strftime("long_gap_multi_%Y%m%dT%H%M%S")
    log_path = output_dir / "long_gap_reduction_multi_trade_audit.log"
    logger = _ensure_logger(log_path)

    logger.info("run_id=%s starting multi-trade long-gap-reduction audit", run_id)
    logger.info("input_results=%s", input_results)
    logger.info("input_candles=%s", input_candles)
    logger.info("recovery_start_purpose=%s", recovery_start_purpose)
    logger.info("recovery_wait_candles=%s", recovery_wait_candles)
    logger.info("dry_run=%s", dry_run)

    if not input_results.exists():
        raise FileNotFoundError(f"continuous_results JSON not found: {input_results}")
    if not input_candles.exists():
        raise FileNotFoundError(f"candle JSON not found: {input_candles}")

    continuous = _load_continuous_results(input_results)
    candles, candle_meta = _load_candles_from_path(input_candles)
    continuous_metadata = dict(continuous.get("metadata") or {})

    slice_resolution, slice_error = resolve_input_slice_start_index(
        continuous_metadata,
        [candle.timestamp for candle in candles],
    )
    if slice_resolution is None:
        raise ValueError(slice_error or "input_slice_start_index_not_resolvable")

    legacy_slice_relative_global = int(continuous_metadata.get("index_semantics_version") or 1) < 2

    runs: List[Dict[str, Any]] = list(continuous.get("runs") or [])
    if not runs:
        raise ValueError("continuous_results JSON contains no runs")

    all_runs_by_trade = {
        int(run.get("trade_number") or 0): run for run in list(continuous.get("runs") or [])
    }
    wait_mode = recovery_wait_candles > 0
    if wait_mode:
        runs = list(continuous.get("runs") or [])
    elif not all_eligible_trades:
        runs = _select_runs(runs, trade_numbers)

    logger.info("total_selected_runs=%d", len(runs))

    symbol = str(continuous.get("metadata", {}).get("symbol") or runs[0].get("symbol") or "")
    base_dir = input_results.parent

    logger.info(
        "resolved input_slice_start_index=%s source=%s",
        slice_resolution.input_slice_start_index,
        slice_resolution.resolution_source,
    )

    eligible_snapshots: List[Dict[str, Any]] = []
    ineligible: List[Dict[str, Any]] = []
    all_trade_diagnostics: List[Dict[str, Any]] = []
    wait_evaluations: List[Dict[str, Any]] = []
    series_last_absolute_index = len(candles) - 1

    for run in runs:
        snapshot, diag = _extract_recovery_snapshot_for_run(
            run,
            recovery_start_purpose=recovery_start_purpose,
            base_dir=base_dir,
            symbol=symbol,
            candles=candles,
            input_slice_start_index=slice_resolution.input_slice_start_index,
            slice_resolution_source=slice_resolution.resolution_source,
            legacy_slice_relative_global=legacy_slice_relative_global,
        )
        diag["recovery_trigger_purpose_reached"] = _purpose_reached_in_trade_blocks(
            base_dir=base_dir,
            symbol=symbol,
            trade_number=int(run.get("trade_number") or 0),
            purpose=recovery_start_purpose,
        )
        trade_number = int(run.get("trade_number") or 0)
        trade_blocks_path = _find_trade_blocks_file_for_run(
            base_dir=base_dir,
            symbol=symbol,
            trade_number=trade_number,
        )
        wait_eval: RecoveryWaitEvaluation | None = None
        if wait_mode:
            if snapshot is None:
                wait_evaluations.append(
                    {
                        "trade_number": trade_number,
                        "recovery_wait_candles": recovery_wait_candles,
                        "recovery_activated": False,
                        "non_activation_reason": "reference_trigger_not_reached",
                        "reference_trigger_reached": False,
                    }
                )
            else:
                wait_eval = evaluate_recovery_wait_activation(
                    run=run,
                    reference_snapshot=snapshot,
                    trade_blocks_path=trade_blocks_path,
                    candles=candles,
                    input_slice_start_index=slice_resolution.input_slice_start_index,
                    recovery_start_purpose=recovery_start_purpose,
                    recovery_wait_candles=recovery_wait_candles,
                    series_last_absolute_index=series_last_absolute_index,
                )
                wait_payload = evaluation_to_dict(wait_eval)
                wait_payload["trade_number"] = trade_number
                wait_payload["reference_trigger_reached"] = True
                wait_evaluations.append(wait_payload)
                diag["wait_activation"] = wait_payload
            all_trade_diagnostics.append(diag)
            if snapshot is None:
                ineligible.append(diag)
                continue
            if wait_eval is not None and not wait_eval.eligible_for_simulation:
                ineligible.append(diag)
                continue
            eligible_snapshots.append(snapshot)
            continue

        all_trade_diagnostics.append(diag)
        if snapshot is None:
            ineligible.append(diag)
            continue
        idx = _snapshot_recovery_index(snapshot)
        if idx < 0 or idx >= len(candles):
            diag["snapshot_validation_errors"].append("recovery_index_out_of_range")
            diag["reason"] = "recovery_index_out_of_range"
            ineligible.append(diag)
            logger.warning(
                "trade_number=%s invalid cycle3_candle_index=%s (candles=%d)",
                snapshot["trade_number"],
                idx,
                len(candles),
            )
            continue
        eligible_snapshots.append(snapshot)

    logger.info(
        "eligible_trades_with_recovery_snapshot=%d purpose=%s",
        len(eligible_snapshots),
        recovery_start_purpose,
    )

    # Preflight JSON always written, even in non-dry runs.
    preflight_path = output_dir / "long_gap_reduction_multi_trade_preflight.json"
    preflight_payload = {
        "run_id": run_id,
        "recovery_start_purpose": recovery_start_purpose,
        "recovery_wait_candles": recovery_wait_candles,
        "recovery_wait_minutes": recovery_wait_candles * CANDLE_INTERVAL_MINUTES,
        "input_results": str(input_results),
        "input_candles": str(input_candles),
        "candle_meta": candle_meta,
        "index_resolution": {
            "input_slice_start_index": slice_resolution.input_slice_start_index,
            "resolution_source": slice_resolution.resolution_source,
            "input_slice_first_timestamp": slice_resolution.input_slice_first_timestamp,
            "legacy_slice_relative_global": legacy_slice_relative_global,
            "index_offset": slice_resolution.input_slice_start_index,
        },
        "total_runs_in_results": len(continuous.get("runs") or []),
        "selected_runs": [int(run.get("trade_number") or 0) for run in runs],
        "eligible_trade_numbers": [snap["trade_number"] for snap in eligible_snapshots],
        "eligible_count": len(eligible_snapshots),
        "activated_trade_numbers": [
            item["trade_number"]
            for item in wait_evaluations
            if item.get("recovery_activated")
        ]
        if wait_mode
        else [snap["trade_number"] for snap in eligible_snapshots],
        "activated_count": len(
            [item for item in wait_evaluations if item.get("recovery_activated")]
        )
        if wait_mode
        else len(eligible_snapshots),
        "trade_diagnostics": all_trade_diagnostics,
        "wait_evaluations": wait_evaluations if wait_mode else None,
        "ineligible_trades": ineligible,
        "dry_run": dry_run,
        "outputs_planned": {
            "orders_csv": str(output_dir / "long_gap_reduction_multi_trade_orders.csv"),
            "events_csv": str(output_dir / "long_gap_reduction_multi_trade_events.csv"),
            "summary_csv": str(output_dir / "long_gap_reduction_multi_trade_summary.csv"),
            "summary_json": str(output_dir / "long_gap_reduction_multi_trade_summary.json"),
            "log": str(log_path),
        },
    }
    preflight_path.write_text(json.dumps(preflight_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("wrote preflight JSON to %s", preflight_path)

    result_paths: Dict[str, Any] = {
        "run_id": run_id,
        "log_path": log_path,
        "preflight_path": preflight_path,
    }

    # In dry-run mode, we stop after preflight.
    if dry_run:
        logger.info("dry_run=True => skipping simulations and output CSV/JSON generation")
        return result_paths

    if wait_mode:
        orders_rows: List[Dict[str, Any]] = []
        events_rows: List[Dict[str, Any]] = []
        summary_rows: List[Dict[str, Any]] = []
        cfg = LongGapReductionConfig(step_trigger_pct=1.0, num_steps=4, fee_rate=None)

        for run in runs:
            trade_number = int(run.get("trade_number") or 0)
            snapshot, _diag = _extract_recovery_snapshot_for_run(
                run,
                recovery_start_purpose=recovery_start_purpose,
                base_dir=base_dir,
                symbol=symbol,
                candles=candles,
                input_slice_start_index=slice_resolution.input_slice_start_index,
                slice_resolution_source=slice_resolution.resolution_source,
                legacy_slice_relative_global=legacy_slice_relative_global,
            )
            wait_eval: RecoveryWaitEvaluation | None = None
            events: List[Dict[str, Any]] | None = None
            lg_summary: Dict[str, Any] | None = None
            recovery_idx: int | None = None

            if snapshot is not None:
                wait_eval = evaluate_recovery_wait_activation(
                    run=run,
                    reference_snapshot=snapshot,
                    trade_blocks_path=_find_trade_blocks_file_for_run(
                        base_dir=base_dir,
                        symbol=symbol,
                        trade_number=trade_number,
                    ),
                    candles=candles,
                    input_slice_start_index=slice_resolution.input_slice_start_index,
                    recovery_start_purpose=recovery_start_purpose,
                    recovery_wait_candles=recovery_wait_candles,
                    series_last_absolute_index=series_last_absolute_index,
                )
                if wait_eval.recovery_activated:
                    recovery_idx = int(wait_eval.recovery_activation_absolute_candle_index)
                    trade_candles = candles[recovery_idx:]
                    events, lg_summary = simulate_long_gap_reduction(
                        candles=trade_candles,
                        start_local_candle_index=0,
                        absolute_start_index=recovery_idx,
                        initial_long_qty=float(wait_eval.activation_long_qty),
                        initial_short_qty=float(wait_eval.activation_short_qty),
                        long_avg=float(wait_eval.activation_long_avg),
                        short_avg=float(wait_eval.activation_short_avg),
                        reference_price=float(wait_eval.activation_reference_price),
                        base_main_realized_pnl=float(wait_eval.activation_base_main_realized_pnl),
                        cfg=cfg,
                    )
                    for idx, ev in enumerate(events):
                        row = dict(ev)
                        row.setdefault("run_id", run_id)
                        row.setdefault("trade_number", trade_number)
                        row.setdefault("event_index", idx)
                        events_rows.append(row)
                    for ev in events:
                        if ev.get("event_type") != "LONG_REDUCE":
                            continue
                        orders_rows.append(
                            {
                                "run_id": run_id,
                                "trade_number": trade_number,
                                "step_index": ev.get("step_index"),
                                "trigger_price": ev.get("trigger_price"),
                                "expected_fill_price": ev.get("expected_fill_price"),
                                "execution_price": ev.get("execution_price"),
                                "reduced_qty": ev.get("reduced_qty"),
                            }
                        )

            summary_rows.append(
                _build_wait_mode_summary_row(
                    run_id=run_id,
                    run=run,
                    recovery_start_purpose=recovery_start_purpose,
                    reference_snapshot=snapshot,
                    wait_eval=wait_eval,
                    events=events,
                    lg_summary=lg_summary,
                    recovery_candle_index=recovery_idx,
                )
            )

        orders_path = output_dir / "long_gap_reduction_multi_trade_orders.csv"
        events_path = output_dir / "long_gap_reduction_multi_trade_events.csv"
        summary_csv_path = output_dir / "long_gap_reduction_multi_trade_summary.csv"
        summary_json_path = output_dir / "long_gap_reduction_multi_trade_summary.json"
        _write_csv(orders_path, orders_rows)
        _write_csv(events_path, events_rows)
        _write_csv(summary_csv_path, summary_rows)
        summary_json_payload = {
            "run_id": run_id,
            "recovery_start_purpose": recovery_start_purpose,
            "recovery_wait_candles": recovery_wait_candles,
            "recovery_wait_minutes": recovery_wait_candles * CANDLE_INTERVAL_MINUTES,
            "trades": summary_rows,
            "inputs": {
                "input_results": str(input_results),
                "input_candles": str(input_candles),
                "candle_meta": candle_meta,
                "recovery_start_purpose": recovery_start_purpose,
                "recovery_wait_candles": recovery_wait_candles,
            },
        }
        summary_json_path.write_text(
            json.dumps(summary_json_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("wrote wait-mode summary for %d trades", len(summary_rows))
        result_paths.update(
            {
                "orders_csv": orders_path,
                "events_csv": events_path,
                "summary_csv": summary_csv_path,
                "summary_json": summary_json_path,
            }
        )
        return result_paths

    if not eligible_snapshots:
        logger.info(
            "no eligible trades with recovery snapshots for purpose=%s; nothing to simulate",
            recovery_start_purpose,
        )
        return result_paths

    # Non-dry-run: perform simulations for each eligible trade.
    orders_rows: List[Dict[str, Any]] = []
    events_rows: List[Dict[str, Any]] = []
    summary_rows: List[Dict[str, Any]] = []

    for snapshot in eligible_snapshots:
        trade_number = snapshot["trade_number"]
        recovery_idx = _snapshot_recovery_index(snapshot)
        trade_candles = candles[recovery_idx:]
        source_run = all_runs_by_trade.get(trade_number, {})

        cfg = LongGapReductionConfig(step_trigger_pct=1.0, num_steps=4, fee_rate=None)
        events, lg_summary = simulate_long_gap_reduction(
            candles=trade_candles,
            start_local_candle_index=0,
            absolute_start_index=recovery_idx,
            initial_long_qty=_snapshot_long_qty(snapshot),
            initial_short_qty=_snapshot_short_qty(snapshot),
            long_avg=_snapshot_long_avg(snapshot),
            short_avg=_snapshot_short_avg(snapshot),
            reference_price=_snapshot_recovery_fill_price(snapshot),
            base_main_realized_pnl=_snapshot_realized_pnl(snapshot),
            cfg=cfg,
        )
        timing = _compute_timing_and_original_comparison(
            run=source_run,
            snapshot=snapshot,
            events=events,
            lg_summary=lg_summary,
            recovery_start_purpose=recovery_start_purpose,
            recovery_candle_index=recovery_idx,
        )

        for idx, ev in enumerate(events):
            row = dict(ev)
            row.setdefault("run_id", run_id)
            row.setdefault("trade_number", trade_number)
            row.setdefault("event_index", idx)
            events_rows.append(row)

        # The orders CSV is intentionally minimal; in this offline model each
        # LONG_REDUCE event corresponds to one synthetic reduce order.
        for ev in events:
            if ev.get("event_type") != "LONG_REDUCE":
                continue
            orders_rows.append(
                {
                    "run_id": run_id,
                    "trade_number": trade_number,
                    "step_index": ev.get("step_index"),
                    "trigger_price": ev.get("trigger_price"),
                    "expected_fill_price": ev.get("expected_fill_price"),
                    "execution_price": ev.get("execution_price"),
                    "reduced_qty": ev.get("reduced_qty"),
                }
            )

        summary_rows.append(
            {
                "run_id": run_id,
                "trade_number": trade_number,
                "recovery_start_purpose": recovery_start_purpose,
                "recovery_candle_index": recovery_idx,
                "cycle3_candle_index": recovery_idx,
                "initial_long_qty": lg_summary.get("initial_long_qty"),
                "initial_short_qty": lg_summary.get("initial_short_qty"),
                "initial_gap_qty": lg_summary.get("initial_gap_qty"),
                "planned_gap_reduce_qty_per_step": lg_summary.get("planned_gap_reduce_qty_per_step"),
                "total_reduced_qty": lg_summary.get("total_reduced_qty"),
                "total_gap_reduction_gross_pnl": lg_summary.get("total_gap_reduction_gross_pnl"),
                "total_gap_reduction_fees": lg_summary.get("total_gap_reduction_fees"),
                "total_gap_reduction_net_pnl": lg_summary.get("total_gap_reduction_net_pnl"),
                "final_long_qty": lg_summary.get("final_long_qty"),
                "final_short_qty": lg_summary.get("final_short_qty"),
                "remaining_gap_qty": lg_summary.get("remaining_gap_qty"),
                "gap_fully_closed": lg_summary.get("gap_fully_closed"),
                **timing,
            }
        )

    orders_path = output_dir / "long_gap_reduction_multi_trade_orders.csv"
    events_path = output_dir / "long_gap_reduction_multi_trade_events.csv"
    summary_csv_path = output_dir / "long_gap_reduction_multi_trade_summary.csv"
    summary_json_path = output_dir / "long_gap_reduction_multi_trade_summary.json"

    _write_csv(orders_path, orders_rows)
    _write_csv(events_path, events_rows)
    _write_csv(summary_csv_path, summary_rows)

    summary_json_payload = {
        "run_id": run_id,
        "recovery_start_purpose": recovery_start_purpose,
        "trades": summary_rows,
        "inputs": {
            "input_results": str(input_results),
            "input_candles": str(input_candles),
            "candle_meta": candle_meta,
            "recovery_start_purpose": recovery_start_purpose,
        },
    }
    summary_json_path.write_text(json.dumps(summary_json_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    logger.info("wrote orders CSV to %s", orders_path)
    logger.info("wrote events CSV to %s", events_path)
    logger.info("wrote summary CSV to %s", summary_csv_path)
    logger.info("wrote summary JSON to %s", summary_json_path)

    result_paths.update(
        {
            "orders_csv": orders_path,
            "events_csv": events_path,
            "summary_csv": summary_csv_path,
            "summary_json": summary_json_path,
        }
    )
    return result_paths


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline multi-trade long-only gap reduction audit.",
    )
    parser.add_argument(
        "--input-results",
        required=True,
        help="Path to *_continuous_results.json for the continuous run.",
    )
    parser.add_argument(
        "--input-candles",
        required=True,
        help="Path to the 5m candle JSON used for the run.",
    )
    parser.add_argument(
        "--output-dir",
        default="research/backtests/results/long_gap_reduction_multi_trade",
        help="Directory where audit outputs will be written.",
    )
    parser.add_argument(
        "--trade-numbers",
        nargs="*",
        type=int,
        help="Optional explicit list of trade numbers to audit.",
    )
    parser.add_argument(
        "--all-eligible-trades",
        action="store_true",
        help="Audit all trades in the results file that have a recovery-start snapshot.",
    )
    parser.add_argument(
        "--recovery-start-purpose",
        default=DEFAULT_RECOVERY_START_PURPOSE,
        help=(
            "Bot purpose whose confirmed fill defines the recovery-start snapshot. "
            f"Allowed: {', '.join(sorted(ALLOWED_RECOVERY_START_PURPOSES))}"
        ),
    )
    parser.add_argument(
        "--recovery-wait-candles",
        type=int,
        default=0,
        help=(
            "Number of 5m candles to wait after the recovery reference fill before "
            "activating gap reduction. 0 keeps the immediate-start behaviour."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only perform preflight validation; do not run simulations.",
    )
    return parser


def main(argv: List[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    try:
        outputs = run_long_gap_reduction_multi_trade_audit(
            input_results=Path(args.input_results),
            input_candles=Path(args.input_candles),
            output_dir=Path(args.output_dir),
            trade_numbers=list(args.trade_numbers) if args.trade_numbers else None,
            all_eligible_trades=bool(args.all_eligible_trades),
            dry_run=bool(args.dry_run),
            recovery_start_purpose=args.recovery_start_purpose,
            recovery_wait_candles=int(args.recovery_wait_candles),
        )
    except Exception as exc:  # pragma: no cover
        print(f"error: {exc}")
        return 1

    print(f"run_id={outputs.get('run_id')}")
    print(f"preflight={outputs.get('preflight_path')}")
    if not args.dry_run:
        print(f"orders_csv={outputs.get('orders_csv')}")
        print(f"events_csv={outputs.get('events_csv')}")
        print(f"summary_csv={outputs.get('summary_csv')}")
        print(f"summary_json={outputs.get('summary_json')}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

