"""Productive short pool resolution (frozen kernel on loaded scanner pane)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from bot.e1r_live_scanner.config import ensure_runtime_paths


def available_for_short_entry(
    symbol: str,
    candles15: list,
    decision_timestamp: datetime,
    *,
    frozen_lld_cache: dict[str, Any],
):
    """
    ``available`` for ``entry_setup`` — frozen config V1, no ClickHouse reload.

    ``ui_lld_config`` / workspace settings are not used here.
    """
    ensure_runtime_paths()
    from dashboard.research_charts.frozen_pool_kernel_v1 import available_from_loaded_pane_v1

    return available_from_loaded_pane_v1(
        symbol,
        candles15,
        decision_timestamp,
        timeframe="15m",
        cache=frozen_lld_cache,
        strict_complete_buckets=True,
    )
