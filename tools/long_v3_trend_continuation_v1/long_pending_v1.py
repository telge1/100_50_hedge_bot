"""Long pending order record."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


def _parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def next_5m_open_after_close(close_utc: str) -> str:
    return _iso(_parse_utc(close_utc) + timedelta(minutes=5))


@dataclass
class LongPendingOrderV1:
    order_id: str
    decision_close_utc: str
    earliest_eligible_execution_utc: str
    pool_lower: float
    pool_upper: float
    pool_key: str
    origin_ids: str
    planned_entry: float
    planned_sl: float
    selection_reason: str | None = None
    lineage_id: str | None = None
    frozen_support_key: str | None = None


@dataclass
class LongPositionV1:
    position_id: str
    entry_price: float
    sl_price: float
    fill_bar_utc: str
    signal_close_utc: str
    pool_key: str
    risk_pct: float | None
