"""Dry-run event / signal payloads."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ScanEvent:
    """One watch / touch / skip / error status for a pool candidate."""

    ts: str
    symbol: str
    event: str  # watch | touch | entry_candidate | skip | idle | error
    side: str = "short"
    rank: int | None = None
    cluster_id: str | None = None
    pool_bottom: float | None = None
    pool_top: float | None = None
    last_price: float | None = None
    dist_to_pool_pct: float | None = None
    ob_ratio: float | None = None
    delta_10m: float | None = None
    flow_confirmed: bool | None = None
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DrySignal:
    """Paper signal that would be sent to the live executor later."""

    ts: str
    symbol: str
    side: str
    rank: int
    cluster_id: str
    entry_price: float
    stop_price: float
    tp_price: float
    pool_bottom: float
    pool_top: float
    touch_ts: str | None
    short_entry_ts: str | None
    ob_ratio_at_touch: float | None
    delta_at_touch: float | None
    flow_confirmed: bool
    tp_room_pct: float | None
    tp_timeframe: str = "5m"
    tp_source: str = "5m_lower_pool"
    dry_run: bool = True
    place_order: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
