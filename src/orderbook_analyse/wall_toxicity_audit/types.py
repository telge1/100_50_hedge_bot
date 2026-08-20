"""Types and parameters for wall toxicity audit."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


AUDIT_VERSION = "wall_toxicity_audit_v1"


class WallToxicityClass(str, Enum):
    EXECUTED_LIQUIDITY = "EXECUTED_LIQUIDITY"
    ABSORPTION_CANDIDATE = "ABSORPTION_CANDIDATE"
    PULLED_BEFORE_TOUCH = "PULLED_BEFORE_TOUCH"
    REMOTE_LIQUIDITY_PULL = "REMOTE_LIQUIDITY_PULL"
    REMOTE_LIQUIDITY_MIGRATION = "REMOTE_LIQUIDITY_MIGRATION"
    NEAR_MARKET_LIQUIDITY_MIGRATION = "NEAR_MARKET_LIQUIDITY_MIGRATION"
    STABLE_PERSISTENT_WALL = "STABLE_PERSISTENT_WALL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SpoofingSuspicion(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(frozen=True)
class WallToxicityParams:
    """Configurable research thresholds (symbol-agnostic)."""

    migration_window_ms: float = 2000.0
    migration_qty_tolerance_pct: float = 25.0
    near_market_bps: float = 30.0
    large_pull_min_qty: float = 50_000.0
    large_pull_min_pct: float = 40.0
    neighbor_buckets: int = 2
    warmup_seconds: float = 30.0
    post_window_seconds: float = 30.0
    touch_bps: float = 5.0
    trade_match_window_ms: float = 500.0
    executed_coverage_min: float = 0.50
    absorption_trade_min_qty: float = 10_000.0
    migration_min_qty: float = 20_000.0
    remote_min_bps: float = 50.0
    tick_size: float | None = None  # optional override; else inferred

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_FORWARD_SECONDS: tuple[int, ...] = (30, 60, 300, 900, 1800)


@dataclass(frozen=True)
class OutcomeParams:
    """Transparent forward-outcome thresholds (documented defaults)."""

    forward_seconds: tuple[int, ...] = DEFAULT_FORWARD_SECONDS
    # Touch: market within this distance (bps) of the wall band edge.
    touch_bps: float = 5.0
    # Break: market crosses beyond the wall band by this extra margin (bps).
    break_bps: float = 5.0
    # Acceptance: remain beyond break level for this duration / sample count.
    acceptance_seconds: float = 15.0
    acceptance_min_samples: int = 3
    # Failed breakout/breakdown: return to original side within this window after break.
    failed_break_return_seconds: float = 60.0
    # Score bins (inclusive upper for HIGH at 100).
    score_low_max: float = 33.0
    score_medium_max: float = 66.0
    # Baseline C: reliability HIGH bin
    reliable_min_score: float = 67.0
    # Baseline D: exclude HIGH suspicion and toxicity above medium
    toxic_exclude_max_score: float = 66.0
    # Lifetime bins
    short_life_seconds: float = 60.0
    long_life_seconds: float = 300.0
    high_removed_without_trade_ratio: float = 0.70
    # Minimum mid samples in forward window for completeness
    min_forward_coverage_ratio: float = 0.50
    # Group sample size below which report marks results as uncertain
    uncertain_sample_n: int = 20

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["forward_seconds"] = list(self.forward_seconds)
        return d


def score_bin(score: float | None, *, low_max: float = 33.0, medium_max: float = 66.0) -> str:
    if score is None:
        return "UNKNOWN"
    if score <= low_max:
        return "LOW"
    if score <= medium_max:
        return "MEDIUM"
    return "HIGH"


@dataclass
class WallSequenceRef:
    symbol: str
    segment_id: str
    wall_sequence_id: str
    side: str  # bid | ask
    resolution: str
    first_seen_ts: datetime
    last_seen_ts: datetime
    closed_ts: datetime | None
    first_price: float
    last_price: float
    min_price: float
    max_price: float
    min_distance_bps: float | None
    max_distance_bps: float | None
    was_near_price: bool
    was_tested: bool
    touched: bool
    disappeared_before_test: bool
    end_reason: str
    first_notional: float | None = None
    last_notional: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LevelQtyEvent:
    """One absolute-quantity update at a price level."""

    ts: datetime
    symbol: str
    side: str
    price: float
    previous_qty: float | None
    new_qty: float
    qty_change: float | None  # new - previous; None if initial unknown
    message_type: str
    update_id: int
    cross_sequence: int
    incomplete_initial: bool
    snapshot_boundary: bool
    in_primary_bucket: bool


@dataclass
class TradeAlignmentRow:
    ts: datetime
    symbol: str
    side: str
    price: float
    qty: float
    notional: float
    in_bucket: bool
    aggressive_vs_wall: bool


@dataclass
class MigrationEvent:
    ts_remove: datetime
    ts_add: datetime
    delay_ms: float
    side: str
    price_from: float
    price_to: float
    distance_ticks: float
    removed_qty: float
    added_qty: float
    matched_qty: float
    toward_market: bool | None
    mid_at_event: float | None
    trade_explained_qty: float


@dataclass
class PullMetrics:
    gross_removed_qty: float = 0.0
    gross_added_qty: float = 0.0
    net_bucket_change: float = 0.0
    removed_without_trade_qty: float = 0.0
    removed_without_trade_ratio: float | None = None
    large_pull_count: int = 0
    largest_single_pull_qty: float = 0.0
    largest_single_pull_pct: float | None = None
    pull_events_before_touch: int = 0
    pull_events_near_touch: int = 0
    trade_qty_in_bucket: float = 0.0
    trade_count_in_bucket: int = 0


@dataclass
class MigrationMetrics:
    migration_event_count: int = 0
    migrated_qty: float = 0.0
    migration_ratio: float | None = None
    median_migration_delay_ms: float | None = None
    median_migration_distance_ticks: float | None = None
    moved_toward_market_qty: float = 0.0
    moved_away_from_market_qty: float = 0.0
    oscillating_liquidity_count: int = 0


@dataclass
class MarketInteraction:
    min_distance_bps: float | None = None
    max_distance_bps: float | None = None
    bucket_touched: bool = False
    trades_in_bucket: bool = False
    removed_before_touch: bool = False
    remained_remote: bool = False
    price_reaction_after_pull_bps: float | None = None


@dataclass
class ScoreComponents:
    persistence_score: float = 0.0
    executed_ratio_score: float = 0.0
    absorption_score: float = 0.0
    refill_score: float = 0.0
    cancellation_before_touch_score: float = 0.0
    order_chasing_score: float = 0.0
    layering_score: float = 0.0
    remote_migration_score: float = 0.0


@dataclass
class WallToxicityResult:
    classification: WallToxicityClass
    reliability_score: float
    toxicity_score: float
    spoofing_suspicion: SpoofingSuspicion
    score_components: ScoreComponents
    pull: PullMetrics
    migration: MigrationMetrics
    market: MarketInteraction
    notes: str = ""
