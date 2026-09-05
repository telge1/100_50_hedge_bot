"""Thresholds and constants — AEF thresholds reused; trap/accept diagnostic only."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip.contracts import (
    AEFConfig,
    PROFILE_UNFITTED_F0_DIAGNOSTIC,
    UNFITTED_F0_DIAGNOSTIC,
    aggressor_side,
)
from orderbook_analyse.aggressor_efficiency_trapped_vwap_acceptance import (
    CAUSAL_CONTRACT_VERSION,
    FEATURE_VERSION,
    PACKAGE_ID,
)

# Wall semantics (reuse wall_toxicity / l2_wall_attack convention).
# ASK wall attacked by aggressive Buy; BID wall attacked by aggressive Sell.
ATTACK_SIDE_BY_WALL = {"ASK": "Buy", "BID": "Sell"}
WALL_BY_AGGRESSOR = {"Buy": "ASK", "Sell": "BID"}

CHECKPOINTS_S = (5, 10, 30, 60, 180)
OUTCOME_HORIZONS_S = (60, 180, 300, 900, 1800, 3600)

# Diagnostic trap confirmation — NOT fitted on outcomes.
TRAP_MIN_UNDERWATER_SHARE = 0.55
TRAP_MIN_CONSECUTIVE_BUCKETS = 3
TRAP_MIN_SECONDS = 3

# Diagnostic edge acceptance — NOT fitted on outcomes.
ACCEPT_MIN_CONSECUTIVE_BUCKETS = 3
ACCEPT_MIN_SECONDS = 3
ACCEPT_MIN_NOTIONAL_BEYOND = 0.0  # transparent raw; gate uses time buckets primarily
EDGE_TOLERANCE_TICKS = 1.0  # tick-size-aware band documented in thresholds_used

EXACT_ON_EDGE_POLICY = "ON_EDGE_NOT_BEYOND"  # trades exactly at edge do not count as break

PROFILE_NAME = "unfitted_trap_accept_v1_diagnostic"


@dataclass(frozen=True)
class TrapAcceptConfig:
    profile_name: str = PROFILE_NAME
    aef_profile: str = PROFILE_UNFITTED_F0_DIAGNOSTIC
    trap_min_underwater_share: float = TRAP_MIN_UNDERWATER_SHARE
    trap_min_consecutive_buckets: int = TRAP_MIN_CONSECUTIVE_BUCKETS
    trap_min_seconds: int = TRAP_MIN_SECONDS
    accept_min_consecutive_buckets: int = ACCEPT_MIN_CONSECUTIVE_BUCKETS
    accept_min_seconds: int = ACCEPT_MIN_SECONDS
    accept_min_notional_beyond: float = ACCEPT_MIN_NOTIONAL_BEYOND
    edge_tolerance_ticks: float = EDGE_TOLERANCE_TICKS
    exact_on_edge_policy: str = EXACT_ON_EDGE_POLICY
    feature_version: str = FEATURE_VERSION
    causal_contract_version: str = CAUSAL_CONTRACT_VERSION
    package_id: str = PACKAGE_ID

    def aef_config(self) -> AEFConfig:
        return AEFConfig.from_profile(self.aef_profile)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["aef_thresholds_source"] = "aggressor_efficiency_flip.contracts.UNFITTED_F0_DIAGNOSTIC"
        d["aef_thresholds"] = dict(UNFITTED_F0_DIAGNOSTIC)
        d["checkpoints_s"] = list(CHECKPOINTS_S)
        d["outcome_horizons_s"] = list(OUTCOME_HORIZONS_S)
        d["attack_side_by_wall"] = dict(ATTACK_SIDE_BY_WALL)
        d["unfitted"] = True
        return d


def wall_side_for_aef_direction(direction: str) -> str:
    """Map AEF LONG/SHORT compression direction to attacked wall side.

    LONG = Sell aggressor compression → typically BID-wall attack context.
    SHORT = Buy aggressor compression → typically ASK-wall attack context.
    This is a semantic prior only — NOT a substitute for a measured pool edge.
    """
    side = aggressor_side(direction)
    return WALL_BY_AGGRESSOR[side]


def aggressor_side_for_wall(wall_side: str) -> str:
    w = str(wall_side).upper()
    if w not in ATTACK_SIDE_BY_WALL:
        raise ValueError(f"unknown wall_side {wall_side!r}")
    return ATTACK_SIDE_BY_WALL[w]
