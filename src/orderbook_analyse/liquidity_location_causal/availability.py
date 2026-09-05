"""Read pool availability from TRP engine objects."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from orderbook_analyse.cluster_sweep_research.clickhouse_source import _timeframe_minutes

from .prefix import utc_naive


def _parse_dt(raw: Any, default: datetime) -> datetime:
    if raw is None:
        return default
    if isinstance(raw, datetime):
        return utc_naive(raw).to_pydatetime()
    return utc_naive(raw).to_pydatetime()


def pool_time_fields(pool: Any) -> dict[str, datetime]:
    """Return v2 availability contract from engine pool (metadata or fallback)."""
    meta = getattr(pool, "metadata", None) or {}
    confirm_open = utc_naive(pool.created_timestamp).to_pydatetime()
    tf = str(pool.timeframe)
    minutes = _timeframe_minutes(tf)
    confirm_end = confirm_open + timedelta(minutes=minutes)
    source_open = utc_naive(pool.source_timestamp).to_pydatetime()
    source_end = source_open + timedelta(minutes=minutes)

    available = _parse_dt(meta.get("available_at"), confirm_end)
    known = _parse_dt(meta.get("known_at"), available)
    max_feat = _parse_dt(meta.get("max_feature_timestamp"), confirm_end)
    return {
        "source_timestamp": source_open,
        "source_bar_start": _parse_dt(meta.get("source_bar_start"), source_open),
        "source_bar_end": _parse_dt(meta.get("source_bar_end"), source_end),
        "confirmation_bar_start": _parse_dt(meta.get("confirmation_bar_start"), confirm_open),
        "confirmation_bar_end": _parse_dt(meta.get("confirmation_bar_end"), confirm_end),
        "available_at": available,
        "known_at": known,
        "max_feature_timestamp": max_feat,
    }


def pool_lifecycle_status(pool: Any, as_of: datetime) -> str:
    """Explicit lifecycle at as_of for one engine pool."""
    t = utc_naive(as_of)
    fields = pool_time_fields(pool)
    avail = utc_naive(fields["available_at"])
    if t < avail:
        return "NOT_YET_KNOWN"
    inv = getattr(pool, "invalidated_timestamp", None)
    if inv is not None and utc_naive(inv) <= t:
        return "INVALIDATED"
    if getattr(pool, "active", True):
        return "ACTIVE"
    return "INVALIDATED"
