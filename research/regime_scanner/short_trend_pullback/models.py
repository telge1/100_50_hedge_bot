"""Dataclasses for short_trend_pullback_v1."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ImpulseState:
    start_bar: int
    end_bar: int
    start_price: float
    end_price: float  # impulse low (short)
    high_at_start: float
    bars: int
    return_pct: float
    atr: float
    atr_move: float
    efficiency: float
    volume_sum: float
    bos_timestamp: Any | None
    protected_high: float | None


@dataclass
class PullbackState:
    start_bar: int
    end_bar: int | None
    high: float
    high_bar: int
    low_after_high: float | None
    low_after_high_bar: int | None
    bars: int
    return_pct: float
    atr_move: float
    retracement: float
    efficiency: float
    volume_sum: float
    volume_ratio: float | None
    internal_bull_bos: bool
    external_bull_choch: bool
    dist_protected_high_pct: float | None
    dist_ema20_pct: float | None
    dist_ema59_pct: float | None


@dataclass
class SetupRuntime:
    context: str
    trigger: str
    state: str = "IDLE"  # IDLE|IMPULSE|PULLBACK|TRIGGERED
    impulse: ImpulseState | None = None
    pullback: PullbackState | None = None
    last_trigger_bar: int | None = None
    invalidate_reason: str | None = None


@dataclass
class SignalEvent:
    symbol: str
    context: str
    trigger: str
    variant: str
    side: str
    trigger_bar: int
    trigger_timestamp: Any
    trigger_price: float  # close of trigger candle
    fill_bar: int
    fill_timestamp: Any
    entry_price: float  # next open
    pullback_high: float
    trigger_level: float
    protected_high: float | None
    invalidation_level: float | None
    distance_to_protected_high: float | None
    pullback_retracement: float
    impulse_strength: float
    regime_variant: str
    features: dict[str, Any] = field(default_factory=dict)
