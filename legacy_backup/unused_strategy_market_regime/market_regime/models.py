from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class FeatureProfileStats:
    mean: float = 0.0
    std: float = 0.0
    p50: float = 0.0
    p75: float = 0.0
    p90: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    n95: float | None = None
    n99: float | None = None
    median: float | None = None
    mad: float | None = None


@dataclass(slots=True)
class CoinProfile:
    symbol: str
    updated_at: datetime | None = None
    sample_size: int = 0
    profile_version: int = 1
    window_start: datetime | None = None
    window_end: datetime | None = None
    features: dict[str, FeatureProfileStats] = field(default_factory=dict)
    threshold_orderflow_long: float = 0.15
    threshold_orderflow_short: float = -0.15
    threshold_buy_sell_imbalance_long: float = 0.20
    threshold_buy_sell_imbalance_short: float = -0.20
    decay_alpha: float | None = None
    profile_mode: str = "rolling"
    notes: str | None = None

    def get_stats(self, feature_name: str) -> FeatureProfileStats:
        return self.features.get(feature_name, FeatureProfileStats())


@dataclass(slots=True)
class RawMarketSnapshot:
    symbol: str
    ts: datetime
    price: float = 0.0
    price_change_1m: float = 0.0
    price_change_5m: float = 0.0
    price_change_15m: float = 0.0
    oi_change: float = 0.0
    oi_change_ratio: float = 0.0
    trade_volume_1m: float = 0.0
    volume_spike_ratio: float = 0.0
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    delta: float = 0.0
    orderflow_ratio: float = 0.0
    liquidation_density_5m: float = 0.0
    liquidation_cluster_score: float = 0.0
    microburst_score: float = 0.0
    spread: float = 0.0
    trade_count_1m: float = 0.0
    avg_trade_size: float | None = None
    atr_1m: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, field_name: str, default: float = 0.0) -> float:
        value = getattr(self, field_name, default)
        if value is None:
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default


@dataclass(slots=True)
class NormalizedSnapshot:
    symbol: str
    ts: datetime
    raw: RawMarketSnapshot
    derived: dict[str, float] = field(default_factory=dict)
    categoricals: dict[str, str] = field(default_factory=dict)
    zscores: dict[str, float] = field(default_factory=dict)
    percentile_flags: dict[str, bool] = field(default_factory=dict)
    missing_inputs: set[str] = field(default_factory=set)

    def value(self, field_name: str, default: float = 0.0) -> float:
        if field_name in self.derived:
            return self.derived[field_name]
        return self.raw.get(field_name, default)

    def z(self, field_name: str, default: float = 0.0) -> float:
        return float(self.zscores.get(field_name, default))

    def label(self, field_name: str, default: str = "") -> str:
        value = self.categoricals.get(field_name, default)
        return str(value) if value is not None else default

    def flag(self, field_name: str) -> bool:
        return bool(self.percentile_flags.get(field_name, False))


@dataclass(slots=True)
class PrimitiveEvents:
    oi_price_build_long: bool = False
    oi_price_short_covering: bool = False
    oi_price_build_short: bool = False
    oi_price_long_flush: bool = False
    price_impulse_up: bool = False
    price_impulse_down: bool = False
    price_extreme_up: bool = False
    price_extreme_down: bool = False
    price_flip_long: bool = False
    price_flip_short: bool = False
    oi_build_up: bool = False
    oi_extreme_build: bool = False
    oi_flush: bool = False
    oi_flip_down: bool = False
    oi_flip_up: bool = False
    volume_participation_high: bool = False
    volume_participation_extreme: bool = False
    trade_participation_surge: bool = False
    large_trade_presence: bool = False
    rebound_participation_surge_long: bool = False
    rebound_participation_surge_short: bool = False
    orderflow_push_long: bool = False
    orderflow_push_short: bool = False
    orderflow_flip_long: bool = False
    orderflow_flip_short: bool = False
    microburst_risk: bool = False
    microburst_extreme: bool = False
    liq_cluster_event: bool = False
    liq_flush_down: bool = False
    liq_flush_up: bool = False
    spread_expansion: bool = False
    spread_explosion: bool = False
    liquidity_vacuum: bool = False
    velocity_slowdown_long: bool = False
    velocity_slowdown_short: bool = False
    pressure_divergence_long: bool = False
    pressure_divergence_short: bool = False
    exhaustion_long: bool = False
    exhaustion_short: bool = False
    volatility_expansion: bool = False
    thin_orderflow_instability: bool = False
    fresh_long_build_up: bool = False
    fresh_short_build_up: bool = False
    high_participation_breakout: bool = False
    weak_move_low_participation: bool = False
    panic_liquidation_phase: bool = False
    squeeze_exhaustion_reversal: bool = False
    spread_stress_phase: bool = False
    dirty_breakout_risk: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {field_name: bool(getattr(self, field_name)) for field_name in self.__dataclass_fields__}


@dataclass(slots=True)
class ScoreSnapshot:
    pressure_score: float
    participation_score: float
    instability_score: float
    exhaustion_score: float
    debug: dict[str, dict[str, float | int | bool]] = field(default_factory=dict)


@dataclass(slots=True)
class SlowRegimeSnapshot:
    state: str
    pressure_score_slow: float
    participation_score_slow: float
    exhaustion_score_slow: float
    oi_price_state: str = "neutral"
    state_memory: str = "slow_range_neutral"
    transition_counter: int = 0
    bias: int = 0
    candidate_flags: dict[str, bool] = field(default_factory=dict)
    transition_reason: list[str] = field(default_factory=list)


@dataclass(slots=True)
class FastTriggerSnapshot:
    state: str
    pressure_score_fast: float
    participation_score_fast: float
    instability_score_fast: float
    exhaustion_score_fast: float
    oi_price_state: str = "neutral"
    candidate_flags: dict[str, bool] = field(default_factory=dict)
    transition_reason: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MidRegimeSnapshot:
    state: str | None
    transition_reason: list[str] = field(default_factory=list)
    candidate_flags: dict[str, bool] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RoutedRegimeSnapshot:
    slow_state: str
    mid_state: str | None
    fast_state: str
    routed_state: str
    oi_price_state: str = "neutral"
    confidence: float = 0.0
    conflict_flags: dict[str, bool] = field(default_factory=dict)
    instability_flags: dict[str, bool] = field(default_factory=dict)
    transition_reason: list[str] = field(default_factory=list)
    emergency_trigger: bool = False
    bot_hint: str = "hold"
    candidate_states: list[str] = field(default_factory=list)
    candidate_flags: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class RegimeSnapshot:
    candidate_states: list[str] = field(default_factory=list)
    candidate_flags: dict[str, bool] = field(default_factory=dict)
    active_state: str = "neutral"
    emergency_trigger: bool = False
    hf_rebound_participation_flag: bool = False
    transition_reason: list[str] = field(default_factory=list)


@dataclass(slots=True)
class StateMachineSnapshot:
    previous_state: str
    current_state: str
    confirmation_counters: dict[str, int] = field(default_factory=dict)
    cooldown_remaining_fast_updates: int = 0
    transition_reason: list[str] = field(default_factory=list)
    transition_applied: bool = False
    slow_state: str | None = None
    slow_state_memory: str | None = None
    slow_transition_counter: int = 0
    slow_bias: int = 0
    mid_state: str | None = None
    fast_state: str | None = None
    routed_state: str | None = None
    current_ts: datetime | None = None
    last_confirmed_ts: datetime | None = None


@dataclass(slots=True)
class MarketSignalResult:
    symbol: str
    ts: datetime
    profile_found: bool
    skipped: bool = False
    skip_reason: str | None = None
    profile: CoinProfile | None = None
    normalized_snapshot: NormalizedSnapshot | None = None
    previous_normalized_snapshot: NormalizedSnapshot | None = None
    events: PrimitiveEvents | None = None
    scores: ScoreSnapshot | None = None
    slow_regime: SlowRegimeSnapshot | None = None
    mid_regime: MidRegimeSnapshot | None = None
    fast_trigger: FastTriggerSnapshot | None = None
    routed_regime: RoutedRegimeSnapshot | None = None
    regime: RegimeSnapshot | None = None
    state_machine: StateMachineSnapshot | None = None
    persisted: bool = False
    decision: str | None = None
    decision_reason: str | None = None
    entry_allowed: bool = False
    confidence_source: str | None = None
    range_unclear_diagnosis: str | None = None
    debug: dict[str, Any] = field(default_factory=dict)
