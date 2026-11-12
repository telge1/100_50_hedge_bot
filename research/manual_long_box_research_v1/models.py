"""Domain types for manual long-box research (read-only drawings)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

WatcherPhase = Literal[
    "IDLE",
    "TOUCH",
    "WATCH",
    "WATCHING",
    "EXPIRED",
    "CONFIRMED",
    "FILL_CANDIDATE",
    "TP_DISTANCE_CHECK",
    "BLOCKED_TP_DISTANCE",
    "FILLED",
    "CLOSED_TP",
    "CLOSED_SL",
    "INVALIDATED",
]


@dataclass(frozen=True)
class LongBoxReject:
    drawing_id: str
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class LongBoxSetup:
    drawing_id: str
    symbol: str
    entry_price: float
    stop_price: float
    target_price: float
    start_ts: int
    end_ts: int
    created_on_timeframe: str
    timeframe_scope: str
    risk_pct: float
    reward_pct: float
    raw: dict[str, Any] = field(repr=False)


@dataclass
class SimEvent:
    bar_idx: int
    utc: int
    drawing_id: str
    phase: WatcherPhase
    message: str
    price: float | None = None


@dataclass
class SimTrade:
    drawing_id: str
    planned_entry: float
    fill_price: float
    stop_price: float
    target_price: float
    fill_utc: int
    exit_utc: int | None
    exit_reason: str
    risk_pct_at_fill: float
    reward_pct_at_fill: float


@dataclass
class ReplayResult:
    boxes: list[LongBoxSetup]
    rejects: list[LongBoxReject]
    events: list[SimEvent]
    trades: list[SimTrade]
    open_decisions: list[str] = field(default_factory=list)
