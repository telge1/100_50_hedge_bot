"""Backward-compatible wrappers around ``lld_research_kernel``."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .lld_research_kernel import (
    DEFAULT_HISTORY_WEEKS,
    compute_lld_selected,
    history_limit_for_timeframe,
    pool_row,
    rows_from_pools,
    ui_lld_config,
)

__all__ = [
    "DEFAULT_HISTORY_WEEKS",
    "history_limit_for_timeframe",
    "ui_lld_config",
    "detect_chart_pools",
    "pool_row",
    "chart_pool_rows",
]


def detect_chart_pools(candles: list, timeframe: str, *, config=None, tip: datetime | None = None):
    """Run LLD like the chart; return pools that would be drawn (not pools_all)."""
    selected, cfg, result, _tip_ts = compute_lld_selected(
        candles, timeframe, config, tip=tip
    )
    return selected, cfg, result


def chart_pool_rows(pools: list, moment: datetime | None = None) -> list[dict]:
    rows: list[dict] = []
    for pool in pools:
        row = pool_row(pool)
        if row is None:
            continue
        if moment is not None and row["known"] > moment:
            continue
        rows.append(row)
    return rows
