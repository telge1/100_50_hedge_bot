"""Typed row schemas for pilot artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


TF_ORDER = {"30m": 0, "1h": 1, "4h": 2}

CONFLUENCE_CLASS_BY_TFS: dict[frozenset[str], str] = {
    frozenset({"30m"}): "C1_30M",
    frozenset({"1h"}): "C1_1H",
    frozenset({"4h"}): "C1_4H",
    frozenset({"30m", "1h"}): "C2_30M_1H",
    frozenset({"30m", "4h"}): "C2_30M_4H",
    frozenset({"1h", "4h"}): "C2_1H_4H",
    frozenset({"30m", "1h", "4h"}): "C3_30M_1H_4H",
}


@dataclass(frozen=True)
class MidTick:
    ts_ns: int
    mid: float
    best_bid: float | None
    best_ask: float | None
    valid: bool
    epoch_id: str
    chunk_key: str


@dataclass(frozen=True)
class MpProfile:
    profile_id: str
    timeframe: str
    profile_start_ts_ns: int
    profile_end_ts_ns: int  # exclusive end = available_at
    profile_available_ts_ns: int
    profile_source: str
    vah: float
    val: float
    poc: float | None
    status: str


@dataclass(frozen=True)
class ActiveLevel:
    level_id: str
    profile_id: str
    timeframe: str
    role: str  # UPPER | LOWER
    level_price: float
    profile_start_ts_ns: int
    profile_end_ts_ns: int
    profile_available_ts_ns: int
    profile_source: str


@dataclass
class ConfluenceZone:
    zone_id: str
    role: str
    confluence_class: str
    confluence_low: float
    confluence_high: float
    confluence_center: float
    confluence_width_bps: float
    timeframes: list[str]
    level_ids: list[str]
    profile_ids: list[str]
    levels: list[float]
    asof_ns: int


@dataclass
class TouchEvent:
    event_id: str
    symbol: str
    first_touch_ts_ns: int
    touch_price: float
    event_role: str
    fade_side: str
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
    timeframe: str  # primary / joined label
    epoch_id: str
    reset_reason: str
    # FSM / geometry
    max_penetration_bps: float = 0.0
    penetration_start_ts_ns: int | None = None
    time_beyond_ms: int = 0
    reclaim_ts_ns: int | None = None
    reclaim_delay_ms: int | None = None
    reclaim_hold_s_observed: float | None = None
    acceptance_time_s: float | None = None
    max_distance_fade_bps: float = 0.0
    max_distance_break_bps: float = 0.0
    # labels
    label: str = "UNRESOLVED"
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
class OutcomeRow:
    event_id: str
    label: str
    fade_side: str
    trigger_ts_ns: int | None
    trigger_price: float | None
    trigger_reason: str
    is_censored: bool
    censor_reason: str
    horizon_s: int
    mfe_bps_gross: float | None
    mae_bps_gross: float | None
    outcome_status: str  # OK | CENSORED | NO_TRIGGER
    tp_sl_results: dict[str, str] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        # flatten tp/sl for csv
        for k, v in self.tp_sl_results.items():
            d[f"tpsl_{k}"] = v
        return d
