"""Phase 3B: frozen pools are productive; legacy workspace path is observer-only."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.e1r_live_scanner.config import ensure_runtime_paths

REASON_MISMATCH = "FROZEN_POOL_PARITY_MISMATCH"
REASON_KERNEL_ERROR = "FROZEN_POOL_KERNEL_ERROR"


def _utc_iso(moment: datetime) -> str:
    ts = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class TransitionParityOutcome:
    block_signals: bool
    reason_code: str | None
    record: dict[str, Any] | None


def compare_legacy_to_frozen_transition(
    symbol: str,
    bar: dict,
    *,
    candles15: list,
    lld_cfg,
    legacy_lld_cache: dict,
    frozen_result,
) -> dict[str, Any]:
    """Read-only legacy vs frozen (frozen is authoritative for production)."""
    ensure_runtime_paths()
    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        scanner_pools_for_index,
    )
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    moment = bar["close_time"]
    idx = int(bar["candle_index"])
    stamps = candle_stamps(candles15)

    legacy_rows, _ui, _proof = scanner_pools_for_index(
        candles15, "15m", idx, lld_cfg, cache=legacy_lld_cache
    )
    legacy_ids = sorted(r["pool_id"] for r in legacy_rows)
    legacy_hash = rows_hash(legacy_rows)
    frozen_ids = list(frozen_result.available_pool_ids)
    frozen_hash = frozen_result.available_rows_hash

    status = "PASS" if legacy_ids == frozen_ids else "MISMATCH"
    record: dict[str, Any] = {
        "symbol": symbol,
        "decision_time": _utc_iso(moment),
        "frozen_count": len(frozen_ids),
        "legacy_count": len(legacy_ids),
        "frozen_hash": frozen_hash,
        "legacy_hash": legacy_hash,
        "status": status,
        "candle_index": frozen_result.candle_index,
        "pane_first": frozen_result.pane_first_timestamp.isoformat(),
        "pane_last": frozen_result.pane_last_timestamp.isoformat(),
        "config_version": frozen_result.frozen_config_version,
    }
    if status == "MISMATCH":
        record["reason_code"] = REASON_MISMATCH
        record["only_frozen"] = sorted(set(frozen_ids) - set(legacy_ids))
        record["only_legacy"] = sorted(set(legacy_ids) - set(frozen_ids))
        record["legacy_candle_index"] = idx
        record["legacy_pane_first"] = stamps[0].isoformat() if stamps else None
        record["legacy_pane_last_open"] = stamps[-1].isoformat() if stamps else None
    return record


def append_transition_log(record: dict[str, Any], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def prime_legacy_transition_cache(
    *,
    candles15: list,
    lld_cfg,
    legacy_lld_cache: dict,
    bar: dict,
) -> None:
    """Advance legacy LLD walker to ``bar`` (loaded pane only; no parity log)."""
    ensure_runtime_paths()
    from dashboard.research_charts.lld_research_kernel import scanner_pools_for_index

    idx = int(bar["candle_index"])
    scanner_pools_for_index(candles15, "15m", idx, lld_cfg, cache=legacy_lld_cache)


def run_transition_parity(
    *,
    enabled: bool,
    log_path: Path,
    symbol: str,
    bar: dict,
    candles15: list,
    lld_cfg,
    legacy_lld_cache: dict,
    frozen_result,
) -> TransitionParityOutcome:
    if not enabled:
        return TransitionParityOutcome(block_signals=False, reason_code=None, record=None)
    record = compare_legacy_to_frozen_transition(
        symbol,
        bar,
        candles15=candles15,
        lld_cfg=lld_cfg,
        legacy_lld_cache=legacy_lld_cache,
        frozen_result=frozen_result,
    )
    append_transition_log(record, log_path)
    if record["status"] == "MISMATCH":
        return TransitionParityOutcome(
            block_signals=True,
            reason_code=REASON_MISMATCH,
            record=record,
        )
    return TransitionParityOutcome(block_signals=False, reason_code=None, record=record)
