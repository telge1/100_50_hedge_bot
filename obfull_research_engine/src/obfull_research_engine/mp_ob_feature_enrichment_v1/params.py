"""Frozen enrichment parameters (no profit tuning)."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


BATCH_RUN_REL = Path("obfull_research_engine/runs/mp_edge_event_batch_v1_20260916")
DEFAULT_RUN_REL = Path("obfull_research_engine/runs/mp_ob_feature_enrichment_v1_20260916")

SILVER_DATABASE = "research_full_ob_silver_v1_3"
METRICS_TABLE = "ob_metrics_100ms_v1_3"
LEVEL_CHANGES_TABLE = "ob_level_changes_v1_3"
PUBLIC_TRADES_TABLE = "orderbook_analysis.public_trades_canonical"
SYMBOL = "BTCUSDT"

NS = 1_000_000_000
BASELINE_BEFORE_S = 120.0
BASELINE_END_BEFORE_S = 30.0
APPROACH_BEFORE_S = 30.0
SHORT_WINDOWS_S = (1.0, 5.0, 15.0, 30.0)
BAND_BPS = (1.0, 2.0, 5.0)
TRADE_MATCH_TOLERANCE_MS = 250
TICK_TOLERANCE_USD = 1.0  # BTCUSDT print noise / tick aggregation band for HIT matching
PILOT_MAX_EVENTS = 20
WALL_PRESENT_FRAC = 0.25  # of baseline zone depth
MAX_LC_PRICE_SPAN_USD = 2500.0

# Chronological Discovery/Validation by COMPLETE windows (fixed before outcomes).
# Windows ordered by start_ts; first 5 ≈ 59% market hours → DISCOVERY; last 2 → VALIDATION.
DISCOVERY_WINDOW_COUNT = 5

# Predeclared feature groups (no outcome-based selection).
FEATURE_GROUPS: dict[str, tuple[str, ...]] = {
    "REFILL_STRENGTH": (
        "refill_ratio",
        "add_after_hit_qty",
        "number_of_refill_cycles",
        "wall_recovery_fraction",
    ),
    "PULL_WEAKNESS": (
        "pull_qty",
        "pull_to_depth_ratio",
        "wall_disappeared_before_touch",
        "wall_disappeared_on_contact",
    ),
    "HIT_ABSORPTION": (
        "hit_qty",
        "hit_to_depth_ratio",
        "hit_notional",
        "wall_min_remaining_fraction",
    ),
    "WALL_PERSISTENCE": (
        "wall_present_baseline_fraction",
        "wall_present_approach_fraction",
        "wall_present_contact_fraction",
        "wall_survival_after_hits",
    ),
    "AGGRESSION_EFFICIENCY": (
        "price_move_bps_per_1m_aggressive_notional",
        "aggression_without_progress",
        "aggressive_notional_per_penetration_bp",
        "aggression_against_zone",
    ),
    "BREAK_ACCEPTANCE": (
        "depth_beyond_1bp",
        "depth_beyond_5bps",
        "aggression_with_break",
        "wall_moved_with_price",
    ),
    "BOOK_IMBALANCE": (
        "imbalance_mean",
        "imbalance_at_touch",
        "imbalance_at_trigger",
        "spread_mean_bps",
    ),
    "WALL_MIGRATION": (
        "wall_price_migration_bps",
        "wall_moved_with_price",
        "wall_disappeared_on_contact",
    ),
}


@dataclass(frozen=True)
class EnrichmentParams:
    batch_run_dir: Path
    out_dir: Path
    silver_database: str = SILVER_DATABASE
    symbol: str = SYMBOL
    pilot_max_events: int = PILOT_MAX_EVENTS
    discovery_window_count: int = DISCOVERY_WINDOW_COUNT
    trade_match_tolerance_ms: int = TRADE_MATCH_TOLERANCE_MS
    tick_tolerance_usd: float = TICK_TOLERANCE_USD
    require_lock_gate: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["batch_run_dir"] = str(self.batch_run_dir)
        d["out_dir"] = str(self.out_dir)
        d["feature_groups"] = {k: list(v) for k, v in FEATURE_GROUPS.items()}
        d["band_bps"] = list(BAND_BPS)
        d["short_windows_s"] = list(SHORT_WINDOWS_S)
        d["baseline_before_s"] = BASELINE_BEFORE_S
        d["baseline_end_before_s"] = BASELINE_END_BEFORE_S
        d["approach_before_s"] = APPROACH_BEFORE_S
        d["tables"] = {
            "metrics": f"{self.silver_database}.{METRICS_TABLE}",
            "level_changes": f"{self.silver_database}.{LEVEL_CHANGES_TABLE}",
            "public_trades": PUBLIC_TRADES_TABLE,
        }
        d["lc_change_types"] = ["ADD", "UPDATE", "DELETE"]
        d["hit_pull_encoding"] = {
            "note": "Silver LC stores ADD|UPDATE|DELETE only; HIT/PULL derived via public-trade matching",
            "HIT": "defense-side size decrease concurrent with aggressive trade at level",
            "PULL": "defense-side size decrease without matching aggressive trade",
            "ADD": "ADD or size increase on defense side",
        }
        return d
