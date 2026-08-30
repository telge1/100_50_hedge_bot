"""Contracts, UNFITTED diagnostic profile, side mapping."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from orderbook_analyse.aggressor_efficiency_flip import (
    CAUSAL_CONTRACT_VERSION,
    FEATURE_VERSION,
)

# Reuse oi_liq_impact_l2 aggressor mapping semantics (LONG compresses Sell).
AGGRESSOR_SIDE_BY_DIRECTION = {
    "LONG": "Sell",
    "SHORT": "Buy",
}
COUNTER_SIDE_BY_DIRECTION = {
    "LONG": "Buy",
    "SHORT": "Sell",
}

CANONICAL_TRADES_TABLE = "orderbook_analysis.public_trades_canonical"
OI_5S_TABLE = "orderbook_analysis.open_interest_5s"

PROFILE_UNFITTED_F0_DIAGNOSTIC = "unfitted_f0_diagnostic"

# Explicit diagnostic thresholds — NOT fitted on DOGE outcomes.
# Documented as UNFITTED / research-only.
UNFITTED_F0_DIAGNOSTIC: dict[str, Any] = {
    "profile_name": PROFILE_UNFITTED_F0_DIAGNOSTIC,
    "status_label": "DIAGNOSTIC_CANDIDATE",
    "flow_seconds": 5,
    "post_flow_seconds": 5,
    "counter_search_seconds": 180,
    "burst_step_seconds": 5,
    "min_dominant_share": 0.60,
    "min_notional_usdt": 10_000.0,
    "min_notional_rank": 0.70,
    "strong_same_side_impact_bps": 8.0,
    "weak_contemporaneous_max_bps": 3.0,
    "strong_post_followthrough_bps": 8.0,
    "reclaim_bps": 3.0,
    "counter_min_notional_usdt": 10_000.0,
    "counter_min_dominant_share": 0.60,
    "counter_min_directional_impact_bps": 3.0,
    "counter_max_immediate_reclaim_bps": 5.0,
    "structure_lookback_seconds": 60,
    "structure_break_eps_bps": 1.0,
    "acceptance_hold_seconds": 5,
    "acceptance_max_reclaim_bps": 5.0,
    "cooldown_seconds": 60,
    "require_structure": True,
    "require_acceptance": True,
    "rank_lookback_bursts": 100,
    "unfitted": True,
    "feature_version": FEATURE_VERSION,
    "causal_contract_version": CAUSAL_CONTRACT_VERSION,
}


@dataclass(frozen=True)
class AEFConfig:
    profile_name: str = PROFILE_UNFITTED_F0_DIAGNOSTIC
    flow_seconds: int = 5
    post_flow_seconds: int = 5
    counter_search_seconds: int = 180
    burst_step_seconds: int = 5
    min_dominant_share: float = 0.60
    min_notional_usdt: float = 10_000.0
    min_notional_rank: float = 0.70
    strong_same_side_impact_bps: float = 8.0
    weak_contemporaneous_max_bps: float = 3.0
    strong_post_followthrough_bps: float = 8.0
    reclaim_bps: float = 3.0
    counter_min_notional_usdt: float = 10_000.0
    counter_min_dominant_share: float = 0.60
    counter_min_directional_impact_bps: float = 3.0
    counter_max_immediate_reclaim_bps: float = 5.0
    structure_lookback_seconds: int = 60
    structure_break_eps_bps: float = 1.0
    acceptance_hold_seconds: int = 5
    acceptance_max_reclaim_bps: float = 5.0
    cooldown_seconds: int = 60
    require_structure: bool = True
    require_acceptance: bool = True
    rank_lookback_bursts: int = 100
    unfitted: bool = True
    feature_version: str = FEATURE_VERSION
    causal_contract_version: str = CAUSAL_CONTRACT_VERSION
    status_label: str = "DIAGNOSTIC_CANDIDATE"

    @classmethod
    def from_profile(cls, name: str) -> "AEFConfig":
        key = str(name or "").strip()
        if key != PROFILE_UNFITTED_F0_DIAGNOSTIC and "unfitted_f0_diagnostic" not in key:
            raise ValueError(
                f"unsupported profile {name!r}; F0 allows only profiles containing "
                f"'unfitted_f0_diagnostic' (got {name!r})"
            )
        raw = dict(UNFITTED_F0_DIAGNOSTIC)
        raw["profile_name"] = key
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__ if k in raw})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def aggressor_side(direction: str) -> str:
    d = str(direction).upper()
    if d not in AGGRESSOR_SIDE_BY_DIRECTION:
        raise ValueError(f"unknown direction {direction!r}")
    return AGGRESSOR_SIDE_BY_DIRECTION[d]


def counter_side(direction: str) -> str:
    d = str(direction).upper()
    if d not in COUNTER_SIDE_BY_DIRECTION:
        raise ValueError(f"unknown direction {direction!r}")
    return COUNTER_SIDE_BY_DIRECTION[d]


def same_side_directional_bps(side: str, raw_bps: float) -> float:
    """Positive = move in the aggressor side's intended direction."""
    if side == "Sell":
        return max(0.0, -float(raw_bps))
    if side == "Buy":
        return max(0.0, float(raw_bps))
    raise ValueError(f"unknown side {side!r}")


def opposite_move_bps(side: str, raw_bps: float) -> float:
    """Positive = move against the aggressor side (rebound)."""
    if side == "Sell":
        return max(0.0, float(raw_bps))
    if side == "Buy":
        return max(0.0, -float(raw_bps))
    raise ValueError(f"unknown side {side!r}")
