"""Types and parameters for EXECUTION_WALL research."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


DETECTOR_VERSION = "execution_wall_detector_v1"


class WallScope(str, Enum):
    STRUCTURE = "STRUCTURE"
    EXECUTION = "EXECUTION"


class WallType(str, Enum):
    STRUCTURE_WALL = "STRUCTURE_WALL"
    EXECUTION_WALL = "EXECUTION_WALL"


class ExecutionState(str, Enum):
    APPEARED = "APPEARED"
    PERSISTED = "PERSISTED"
    GREW = "GREW"
    SHRANK = "SHRANK"
    MOVED_TOWARD_MARKET = "MOVED_TOWARD_MARKET"
    MOVED_AWAY_FROM_MARKET = "MOVED_AWAY_FROM_MARKET"
    TOUCHED = "TOUCHED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    REFILLED = "REFILLED"
    ABSORBING = "ABSORBING"
    CONSUMED = "CONSUMED"
    PULLED_BEFORE_TOUCH = "PULLED_BEFORE_TOUCH"
    BROKEN = "BROKEN"
    ACCEPTED_BEYOND_LEVEL = "ACCEPTED_BEYOND_LEVEL"
    FAILED_BREAK = "FAILED_BREAK"
    DISAPPEARED = "DISAPPEARED"


DEFAULT_DISTANCE_BANDS_BPS: tuple[float, ...] = (0.0, 5.0, 10.0, 20.0, 30.0, 50.0)
DEFAULT_FORWARD_SECONDS: tuple[int, ...] = (5, 15, 30, 60, 300)


@dataclass(frozen=True)
class ExecutionWallParams:
    """Transparent, configurable Execution-Wall thresholds (not fitted to one case)."""

    max_distance_bps: float = 30.0
    distance_bands_bps: tuple[float, ...] = DEFAULT_DISTANCE_BANDS_BPS
    bucket_mode: str = "ticks"  # exact | ticks | bps
    bucket_ticks: int = 1
    bucket_bps: float = 2.0
    sample_interval_ms: float = 500.0
    local_radius_ticks: int = 8
    local_multiple_min: float = 3.0
    local_percentile_min: float = 95.0
    local_depth_share_min: float = 0.10
    # Extra near-touch band: softer local bar so BBO-adjacent walls are not
    # dominated by larger resting size at 20–30 bps inside the same max band.
    near_touch_bps: float = 10.0
    near_touch_percentile_min: float = 80.0
    near_touch_multiple_min: float = 2.0
    near_touch_rank_max: int = 3
    min_level_qty: float = 50.0
    min_level_notional: float = 25.0
    min_lifetime_ms: float = 250.0
    match_price_ticks: float = 1.5
    match_qty_tolerance_pct: float = 40.0
    touch_bps: float = 5.0
    break_bps: float = 5.0
    touch_ticks: float | None = 2.0
    break_ticks: float | None = 2.0
    acceptance_seconds: float = 15.0
    failed_break_return_seconds: float = 60.0
    trade_match_window_ms: float = 400.0
    absorption_exec_to_peak_min: float = 0.25
    absorption_max_progress_bps: float = 8.0
    absorption_min_refill_ratio: float = 0.15
    chunk_minutes: float = 15.0
    forward_seconds: tuple[int, ...] = DEFAULT_FORWARD_SECONDS
    tick_size: float | None = None
    structure_sequences_csv: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["distance_bands_bps"] = list(self.distance_bands_bps)
        d["forward_seconds"] = list(self.forward_seconds)
        return d


@dataclass
class LocalLevelMetrics:
    side: str
    price: float
    bucket_price: float
    level_qty: float
    level_notional: float
    distance_bps: float
    same_side_near_depth: float
    same_side_local_median_qty: float
    same_side_local_mean_qty: float
    same_side_local_percentile: float
    opposite_side_near_depth: float
    local_depth_share: float
    local_multiple: float
    book_imbalance_near: float
    level_rank_within_near_band: int
    is_candidate: bool
    band_label: str


@dataclass
class ExecutionWallSequence:
    wall_sequence_id: str
    symbol: str
    side: str
    wall_type: str = WallType.EXECUTION_WALL.value
    wall_scope: str = WallScope.EXECUTION.value
    representative_price: float = 0.0
    price_min: float = 0.0
    price_max: float = 0.0
    first_seen: datetime | None = None
    last_active: datetime | None = None
    disappeared_at: datetime | None = None
    lifetime_ms: float = 0.0
    initial_qty: float = 0.0
    peak_qty: float = 0.0
    last_qty: float = 0.0
    min_distance_bps: float | None = None
    max_distance_bps: float | None = None
    time_near_market_ms: float = 0.0
    touch_time: datetime | None = None
    break_time: datetime | None = None
    touch_status: str = "UNTOUCHED"
    terminal_state: str = ExecutionState.APPEARED.value
    sample_count: int = 0
    local_multiple_peak: float = 0.0
    local_percentile_peak: float = 0.0
    executed_qty_estimate: float = 0.0
    cancelled_or_pulled_qty_estimate: float = 0.0
    unexplained_removed_qty: float = 0.0
    refilled_qty: float = 0.0
    refill_count: int = 0
    pulled_before_touch: bool = False
    absorption_candidate: bool = False
    breakout_attempted: bool = False
    breakout_accepted: bool = False
    breakout_failed: bool = False
    execution_alignment_status: str = "OK"
    notes: str = ""
    transitions: list[dict[str, Any]] = field(default_factory=list)
