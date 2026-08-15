"""Dataclasses for liquidity_sweep_reclaim_v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SetupState = Literal[
    "LEVEL_ELIGIBLE",
    "SWEPT",
    "RECLAIMED",
    "CONFIRMED",
    "TRIGGERED",
    "FILLED",
    "INVALIDATED",
]


@dataclass
class LevelSnapshot:
    level_family: str
    level_id: str
    level_value: float
    side: str  # long | short (sweep side that uses this level)
    confirmed_timestamp: str
    confirmed_bar: int
    age_bars: int
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class SetupRuntime:
    setup_id: str
    variant: str
    level_family: str
    penetration_class: str
    reclaim_type: str
    side: str
    level_id: str
    level_value: float
    level_confirmed_timestamp: str
    level_confirmed_bar: int
    state: SetupState = "LEVEL_ELIGIBLE"
    sweep_timestamp: str | None = None
    sweep_bar: int | None = None
    sweep_extreme: float | None = None
    penetration_atr: float | None = None
    penetration_pct: float | None = None
    reclaim_timestamp: str | None = None
    reclaim_bar: int | None = None
    confirmation_timestamp: str | None = None
    confirmation_bar: int | None = None
    trigger_timestamp: str | None = None
    trigger_bar: int | None = None
    fill_timestamp: str | None = None
    fill_bar: int | None = None
    entry_price: float | None = None
    invalidation_reason: str | None = None
    level_meta: dict[str, Any] = field(default_factory=dict)
    sweep_meta: dict[str, Any] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)


@dataclass
class SignalEvent:
    setup_id: str
    symbol: str
    variant: str
    level_family: str
    penetration_class: str
    reclaim_type: str
    side: str
    level_id: str
    level_value: float
    level_confirmed_timestamp: str
    sweep_timestamp: str
    reclaim_timestamp: str
    confirmation_timestamp: str | None
    trigger_timestamp: str
    fill_timestamp: str
    trigger_bar: int
    fill_bar: int
    entry_price: float
    trigger_price: float
    penetration_atr: float
    penetration_pct: float
    bars_sweep_to_reclaim: int
    bars_reclaim_to_trigger: int
    setup_age: int
    features: dict[str, Any] = field(default_factory=dict)
