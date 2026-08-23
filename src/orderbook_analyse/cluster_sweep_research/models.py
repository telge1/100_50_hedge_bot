from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class SetupDirection(str, Enum):
    BULLISH = "BULLISH"  # long: lower/support cluster sweep
    BEARISH = "BEARISH"  # short: upper/resistance cluster sweep


class EventState(str, Enum):
    CLUSTER_APPROACH = "CLUSTER_APPROACH"
    CLUSTER_ENTRY = "CLUSTER_ENTRY"
    PRICE_CROSSED_EMA59 = "PRICE_CROSSED_EMA59"
    EMA_STRUCTURE_INTACT = "EMA_STRUCTURE_INTACT"
    CLUSTER_HOLD = "CLUSTER_HOLD"
    CLUSTER_BREAK = "CLUSTER_BREAK"
    RECLAIM_PENDING = "RECLAIM_PENDING"
    RECLAIM_CONFIRMED = "RECLAIM_CONFIRMED"
    REJECTION_CONFIRMED = "REJECTION_CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


class ConfirmationVariant(str, Enum):
    CLOSE_BACK_IN_CLUSTER = "CLOSE_BACK_IN_CLUSTER"
    CLOSE_BEYOND_CLUSTER_EDGE = "CLOSE_BEYOND_CLUSTER_EDGE"
    CLOSE_RECLAIM_EMA59 = "CLOSE_RECLAIM_EMA59"
    ORDERFLOW_REVERSAL = "ORDERFLOW_REVERSAL"
    CLUSTER_AND_EMA_RECLAIM = "CLUSTER_AND_EMA_RECLAIM"


@dataclass(frozen=True)
class ClusterSnapshot:
    cluster_id: str
    side: str  # upper | lower
    low: float
    high: float
    mid: float
    width_abs: float
    width_pct: float | None
    pool_count: int  # chart number when >= minimum_cluster_pools
    strength_sum: float | None
    strength_mean: float | None
    strength_max: float | None
    oldest_created: datetime
    newest_created: datetime
    pool_ids: tuple[str, ...]
    chart_label_meaning: str = (
        "pool_count: number of same-side LLD pools merged into this cluster "
        "(display filter typically minimum_cluster_pools=3)"
    )


@dataclass
class SweepEvent:
    event_id: str
    setup_direction: SetupDirection
    symbol: str
    timeframe: str
    cluster: ClusterSnapshot
    states: list[EventState] = field(default_factory=list)
    t_approach: datetime | None = None
    t_first_touch: datetime | None = None
    t_entry: datetime | None = None
    t_max_sweep: datetime | None = None
    t_price_cross_ema59: datetime | None = None
    t_reclaim_or_reject: datetime | None = None
    t_earliest_entry: datetime | None = None
    t_invalidated: datetime | None = None
    features: dict[str, Any] = field(default_factory=dict)
    confirmations: dict[str, Any] = field(default_factory=dict)
    outcomes: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)
    causality_note: str = (
        "All decision fields use only information known at that bar close (UTC)."
    )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["setup_direction"] = self.setup_direction.value
        d["states"] = [s.value for s in self.states]
        return d
