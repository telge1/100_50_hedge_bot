"""Typed configs and result models for Breakout X-Ray V1."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class RunMode(str, Enum):
    STRATEGY_EDGE = "strategy-edge"
    MANUAL_WINDOW = "manual-window"


class ReferenceSource(str, Enum):
    MARKET_PROFILE_30M = "market_profile_30m"
    MANUAL_OVERRIDE = "manual_override"
    MANUAL_FORENSIC = "manual_forensic"
    NONE = "none"


class EdgeSide(str, Enum):
    UPPER = "upper"
    LOWER = "lower"


class WallClass(str, Enum):
    REFILL = "REFILL"
    ABSORPTION = "ABSORPTION"
    CONSUMPTION = "CONSUMPTION"
    PULL = "PULL"
    RELOCATION = "RELOCATION"
    GROWTH = "GROWTH"
    DECAY = "DECAY"
    UNRESOLVED = "UNRESOLVED"
    UNRESOLVED_BASELINE = "UNRESOLVED_BASELINE"


class BreakoutState(str, Enum):
    APPROACH = "APPROACH"
    ATTACK = "ATTACK"
    TOUCH = "TOUCH"
    HOLD = "HOLD"
    RECLAIM = "RECLAIM"
    BREAK = "BREAK"
    ACCEPTED_BREAK = "ACCEPTED_BREAK"
    FAILED_BREAK = "FAILED_BREAK"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True)
class ReferenceLevel:
    price: float | None
    side: EdgeSide | None
    source: ReferenceSource
    known_as_of_utc: datetime | None
    causal_reference: bool
    profile_window_start: datetime | None = None
    profile_window_end: datetime | None = None
    formula_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = None if self.side is None else self.side.value
        d["source"] = self.source.value
        for key in ("known_as_of_utc", "profile_window_start", "profile_window_end"):
            v = d.get(key)
            if isinstance(v, datetime):
                d[key] = v.isoformat().replace("+00:00", "Z")
        return d


@dataclass(frozen=True)
class AnalysisWindow:
    symbol: str
    start_utc: datetime
    end_utc: datetime
    start_ns: int
    end_ns: int
    chain_version: str
    chain_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "start_utc": self.start_utc.isoformat().replace("+00:00", "Z"),
            "end_utc": self.end_utc.isoformat().replace("+00:00", "Z"),
            "start_ns": self.start_ns,
            "end_ns": self.end_ns,
            "chain_version": self.chain_version,
            "chain_hash": self.chain_hash,
        }


@dataclass(frozen=True)
class WallThresholdSpec:
    method: str
    window_start: datetime
    window_end: datetime
    known_as_of_utc: datetime
    causal: bool
    quantile: float = 0.95
    local_band_usd: float = 400.0
    max_rank: int = 12

    def to_dict(self) -> dict[str, Any]:
        return {
            "wall_threshold_method": self.method,
            "wall_threshold_window_start": self.window_start.isoformat().replace("+00:00", "Z"),
            "wall_threshold_window_end": self.window_end.isoformat().replace("+00:00", "Z"),
            "wall_threshold_known_as_of_utc": self.known_as_of_utc.isoformat().replace(
                "+00:00", "Z"
            ),
            "wall_threshold_causal": self.causal,
            "quantile": self.quantile,
            "local_band_usd": self.local_band_usd,
            "max_rank": self.max_rank,
        }


@dataclass(frozen=True)
class BaselineBookState:
    source: str
    timestamp: datetime | None
    age_ms: float | None
    complete: bool
    hash: str | None
    unresolved_reason: str | None = None
    levels_in_band: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_state_source": self.source,
            "baseline_state_timestamp": None
            if self.timestamp is None
            else self.timestamp.isoformat().replace("+00:00", "Z"),
            "baseline_state_age_ms": self.age_ms,
            "baseline_state_complete": self.complete,
            "baseline_state_hash": self.hash,
            "unresolved_reason": self.unresolved_reason,
            "levels_in_band_count": len(self.levels_in_band),
        }


@dataclass(frozen=True)
class StrategyEdgeConfig:
    symbol: str
    decision_time_utc: datetime
    edge_side: EdgeSide
    pre_window_minutes: int
    post_window_minutes: int
    local_band_usd: float
    output_dir: str
    check_only: bool = False
    reference_price: float | None = None
    reference_known_as_of_utc: datetime | None = None
    chain_version: str = (
        "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
    )
    chain_hash: str = (
        "f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333"
    )
    input_database: str = "research_full_ob_continuous_v1_3"
    output_database: str = "research_full_ob_silver_v1_3"
    wall_quantile: float = 0.95
    wall_max_rank: int = 12


@dataclass(frozen=True)
class ManualWindowConfig:
    symbol: str
    start_utc: datetime
    end_utc: datetime
    local_band_usd: float
    output_dir: str
    check_only: bool = False
    reference_price: float | None = None
    reference_side: EdgeSide | None = None
    reference_known_as_of_utc: datetime | None = None
    chain_version: str = (
        "canonical_segment_chain_v1_3_BTCUSDT_20260912T060011Z_f666e592a0bef459"
    )
    chain_hash: str = (
        "f666e592a0bef4598545b3f247cf5dd97dd9c53017028e3c33ddfce5e0d15333"
    )
    input_database: str = "research_full_ob_continuous_v1_3"
    output_database: str = "research_full_ob_silver_v1_3"
    wall_quantile: float = 0.95
    wall_max_rank: int = 12


@dataclass
class OutcomeHorizon:
    label: str
    status: str  # EVALUATED | UNAVAILABLE_OUTSIDE_WINDOW
    required_end_utc: datetime
    evaluated: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "required_end_utc": self.required_end_utc.isoformat().replace("+00:00", "Z"),
            "evaluated": self.evaluated,
            "detail": self.detail,
        }


@dataclass
class XRayResult:
    mode: RunMode
    window: AnalysisWindow
    reference: ReferenceLevel
    wall_threshold: WallThresholdSpec
    baseline: BaselineBookState
    sections: dict[str, Any] = field(default_factory=dict)
    data_quality: dict[str, Any] = field(default_factory=dict)
    report_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "window": self.window.to_dict(),
            "reference": self.reference.to_dict(),
            "wall_threshold": self.wall_threshold.to_dict(),
            "baseline": self.baseline.to_dict(),
            "sections": self.sections,
            "data_quality": self.data_quality,
            "report_hash": self.report_hash,
        }
