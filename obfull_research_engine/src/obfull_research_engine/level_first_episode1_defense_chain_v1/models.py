"""Typed DefenseChainEvent / DefenseChainNode structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from . import CHAIN_CALIBRATION_STATUS, CHAIN_STATUS_EP1, TIME_BASIS


@dataclass
class DefenseChainNode:
    chain_node_index: int
    wall_generation_id: str
    wall_price: float
    first_visible_at: str
    first_relevant_at: str | None
    first_trade_touch: str | None
    end_time: str | None
    initial_qty: float
    peak_qty: float
    final_qty: float
    rolling_size_percentile_at_entry: float | None
    pre_existing: bool
    cumulative_hits: float
    cumulative_pulls: float
    cumulative_refills: float
    qdh_base_start: float | None
    qdh_base_peak: float | None
    qdh_base_end: float | None
    attacker_persistence_start: float | None
    attacker_persistence_end: float | None
    price_at_entry: float | None
    price_at_exit: float | None
    microprice_at_entry: float | None
    microprice_at_exit: float | None
    termination_reason: str
    next_node_delay_ms: int | None
    price_advance_to_next_node_ticks: float | None
    attribution_confidence: str
    coverage_ok: bool
    transition_to_next: str | None = None
    impact_efficiency_start: float | None = None
    impact_efficiency_end: float | None = None
    layer_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DefenseChainEvent:
    chain_id: str
    episode_id: str
    symbol: str
    replay_epoch: int
    zone_id: str
    zone_side: str
    attack_direction: str
    chain_start: str
    chain_end: str | None
    max_input_available_at: str
    coverage_ok: bool
    censor_reason: str | None
    node_count: int
    attacked_node_count: int
    pre_existing_node_count: int
    new_post_breach_node_count: int
    first_wall_price: float | None
    current_front_wall_price: float | None
    highest_defense_price: float | None
    chain_advance_ticks: float | None
    chain_advance_bps: float | None
    cumulative_initial_wall_qty: float
    cumulative_peak_wall_qty: float
    cumulative_attributed_fill_qty: float
    cumulative_residual_pull_qty: float
    cumulative_refill_qty: float
    cumulative_attack_notional: float
    cumulative_counterflow_notional: float
    price_progress_ticks: float | None
    price_progress_bps: float | None
    microprice_progress_ticks: float | None
    attack_rate_fast: float | None
    attack_rate_slow: float | None
    attacker_persistence_ratio: float | None
    counterflow_rate_fast: float | None
    counterflow_rate_slow: float | None
    active_wall_count: int
    nearest_relevant_wall_distance_ticks: float | None
    ask_liquidity_centroid: float | None
    centroid_shift_ticks: float | None
    chain_status: str = CHAIN_STATUS_EP1
    calibration_status: str = CHAIN_CALIBRATION_STATUS
    look_ahead: bool = False
    time_basis: str = TIME_BASIS
    nodes: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
