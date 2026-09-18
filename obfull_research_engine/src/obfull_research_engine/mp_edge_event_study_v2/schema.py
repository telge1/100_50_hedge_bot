"""V2 schemas."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class RejectedTouch:
    ts_ns: int
    zone_id: str
    event_role: str
    confluence_class: str
    mid: float
    confluence_low: float
    confluence_high: float
    reason: str
    approach_side: str = ""
    armed: bool = False

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EventV2:
    event_id: str
    symbol: str
    first_touch_ts_ns: int
    touch_price: float
    event_role: str
    label_price_only: str
    fade_side: str
    break_side: str
    trade_side: str
    trade_side_reason: str
    zone_id: str
    confluence_class: str
    confluence_low: float
    confluence_high: float
    confluence_center: float
    confluence_width_bps: float
    timeframes: list[str]
    active_profile_ids: list[str]
    level_ids: list[str]
    profile_start_ts_ns: int | None
    profile_end_ts_ns: int | None
    profile_available_ts_ns: int | None
    profile_source: str
    timeframe: str
    epoch_id: str
    # approach
    approach_side: str = ""
    approach_start_price: float | None = None
    approach_distance_bps: float | None = None
    armed_ts_ns: int | None = None
    rejected_touch_reason: str = ""
    # geometry / timing
    max_penetration_bps: float = 0.0
    penetration_ts_ns: int | None = None
    first_acceptance_ts_ns: int | None = None
    reclaim_ts_ns: int | None = None
    confirmed_reclaim_ts_ns: int | None = None
    true_break_confirmation_ts_ns: int | None = None
    time_beyond_ms: int = 0
    reclaim_hold_s_observed: float | None = None
    acceptance_time_s: float | None = None
    max_distance_fade_bps: float = 0.0
    max_distance_break_bps: float = 0.0
    transition_pattern: str = ""
    # labels / censor
    is_censored: bool = False
    censor_reason: str = ""
    available_forward_s: float = 0.0
    trigger_ts_ns: int | None = None
    trigger_price: float | None = None
    trigger_reason: str = ""
    fsm_end_state: str = ""

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class OutcomeV2:
    event_id: str
    label_price_only: str
    event_role: str
    fade_side: str
    break_side: str
    trade_side: str
    trade_side_reason: str
    trigger_ts_ns: int | None
    trigger_price: float | None
    trigger_reason: str
    is_censored: bool
    censor_reason: str
    horizon_s: int
    mfe_bps_gross: float | None
    mae_bps_gross: float | None
    outcome_status: str
    tp_sl_results: dict[str, str] = field(default_factory=dict)
    v1_true_break_outcomes_invalid: bool = False

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        for k, v in self.tp_sl_results.items():
            d[f"tpsl_{k}"] = v
        return d


@dataclass
class RejectionStats:
    raw_touch_candidates: int = 0
    valid_directional_touches: int = 0
    rejected_wrong_approach: int = 0
    rejected_not_armed: int = 0
    rejected_duplicate: int = 0
    rejected_profile_activation_inside_zone: int = 0

    def to_dict(self) -> dict[str, int]:
        return asdict(self)
