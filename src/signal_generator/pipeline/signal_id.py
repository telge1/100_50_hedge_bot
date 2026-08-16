"""Deterministic signal UUID (uuid5) for idempotent re-runs."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

# Fixed namespace for this project (not random).
_SIGNAL_NS = uuid.UUID("a7c3e91f-2b54-4d6a-9e08-1f5d3c8b7a20")


def _utc_iso_minute(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def deterministic_signal_id(
    *,
    strategy_version: str,
    symbol: str,
    timeframe: str,
    candle_open_time: datetime,
    direction: str,
    signal_type: str,
) -> uuid.UUID:
    """Stable logical id across restarts / re-runs."""
    key = "|".join(
        [
            strategy_version,
            symbol.upper(),
            timeframe,
            _utc_iso_minute(candle_open_time),
            direction.upper(),
            signal_type,
        ]
    )
    return uuid.uuid5(_SIGNAL_NS, key)
