"""Shadow parity: current Short pool path vs frozen_pool_kernel_v1 (no strategy side effects)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bot.e1r_live_scanner.config import ensure_runtime_paths


def _utc_iso(moment: datetime) -> str:
    ts = moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def compare_frozen_pool_shadow(
    symbol: str,
    bar: dict,
    *,
    candles15: list,
    lld_cfg,
    lld_cache: dict,
    frozen_shadow_cache: dict,
) -> dict[str, Any]:
    """Read-only pool parity. Does not touch seen sets, registry, or signals."""
    ensure_runtime_paths()
    from dashboard.research_charts.frozen_lld_config_v1 import PANE_FROM_UTC
    from dashboard.research_charts.frozen_pool_kernel_v1 import available_from_loaded_pane_v1
    from dashboard.research_charts.lld_research_kernel import (
        candle_stamps,
        scanner_pools_for_index,
    )
    from dashboard.research_charts.short_pool_oracle_v1 import rows_hash

    moment = bar["close_time"]
    idx = int(bar["candle_index"])
    stamps = candle_stamps(candles15)

    current, _ui, _proof = scanner_pools_for_index(
        candles15, "15m", idx, lld_cfg, cache=lld_cache
    )
    current_ids = sorted(r["pool_id"] for r in current)
    current_hash = rows_hash(current)

    frozen = available_from_loaded_pane_v1(
        symbol,
        candles15,
        moment,
        timeframe="15m",
        cache=frozen_shadow_cache,
    )

    status = "PASS" if current_ids == frozen.available_pool_ids else "MISMATCH"
    record: dict[str, Any] = {
        "symbol": symbol,
        "decision_time": _utc_iso(moment),
        "current_count": len(current_ids),
        "frozen_count": len(frozen.available_pool_ids),
        "current_hash": current_hash,
        "frozen_hash": frozen.available_rows_hash,
        "status": status,
        "current_candle_index": idx,
        "frozen_candle_index": frozen.candle_index,
        "current_pane_first": stamps[0].isoformat() if stamps else None,
        "frozen_pane_first": frozen.pane_first_timestamp.isoformat(),
        "current_pane_last_open": stamps[-1].isoformat() if stamps else None,
        "frozen_pane_last_open": frozen.pane_last_timestamp.isoformat(),
        "pane_from_expected": PANE_FROM_UTC.isoformat(),
    }
    if status == "MISMATCH":
        record["only_current"] = sorted(set(current_ids) - set(frozen.available_pool_ids))
        record["only_frozen"] = sorted(set(frozen.available_pool_ids) - set(current_ids))
    return record


def append_parity_log(record: dict[str, Any], log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def maybe_run_frozen_pool_shadow_parity(
    *,
    enabled: bool,
    log_path: Path,
    symbol: str,
    bar: dict,
    candles15: list,
    lld_cfg,
    lld_cache: dict,
    frozen_shadow_cache: dict,
) -> None:
    if not enabled:
        return
    record = compare_frozen_pool_shadow(
        symbol,
        bar,
        candles15=candles15,
        lld_cfg=lld_cfg,
        lld_cache=lld_cache,
        frozen_shadow_cache=frozen_shadow_cache,
    )
    append_parity_log(record, log_path)
