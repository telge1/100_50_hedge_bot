"""Evaluate minimum raw distance to next opposing liquidity pool in TP direction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

from orderbook_analyse.liquidity_pool_min_target_distance_config_v1 import HTF_TIMEFRAMES
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    RoomGateConfigError,
    RoomToTargetConfig,
)


@dataclass(frozen=True)
class PoolCandidate:
    pool_id: str
    source_timeframe: str
    side: str
    lower_edge: float
    upper_edge: float
    available_at: str
    active_as_of: bool = True


def _utc(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _pool_contains_price(pool: PoolCandidate, price: float) -> bool:
    return pool.lower_edge <= price <= pool.upper_edge


def _distance_pct(direction: str, entry_price: float, target_price: float) -> float:
    if direction == "LONG":
        return ((target_price - entry_price) / entry_price) * 100.0
    return ((entry_price - target_price) / entry_price) * 100.0


def _compare_distance(raw_pct: float, config: RoomToTargetConfig) -> bool:
    if config.comparison == "greater_than_or_equal":
        return raw_pct >= config.min_target_distance_pct
    raise RoomGateConfigError(f"unsupported comparison: {config.comparison}")


EXECUTION_TIMEFRAMES = frozenset({"5m"})


def _entry_inside_opposing(direction: str, entry_price: float, pools: Sequence[PoolCandidate]) -> bool:
    """Entry inside an execution-TF (5m) opposing pool."""
    opposing = "ASK" if direction == "LONG" else "BID"
    return any(
        p.side == opposing
        and p.source_timeframe in EXECUTION_TIMEFRAMES
        and _pool_contains_price(p, entry_price)
        for p in pools
    )


def _htf_opposing_overlap(direction: str, entry_price: float, pools: Sequence[PoolCandidate]) -> bool:
    opposing = "ASK" if direction == "LONG" else "BID"
    return any(
        p.side == opposing
        and p.source_timeframe in HTF_TIMEFRAMES
        and _pool_contains_price(p, entry_price)
        for p in pools
    )


def _cost_room_fields(raw_bps: float, config: RoomToTargetConfig) -> dict[str, float]:
    out: dict[str, float] = {}
    for bps in config.cost_scenarios_bps:
        key = f"room_after_cost_{int(bps)}bps"
        out[key] = raw_bps - bps
    return out


def evaluate_room_to_target_gate(
    *,
    direction: str,
    entry_price: float,
    pools: Sequence[PoolCandidate],
    config: RoomToTargetConfig,
    as_of_iso: str | None = None,
) -> dict[str, Any]:
    """Return full room-gate audit payload for a long/short candidate."""
    direction = direction.upper()
    if direction not in {"LONG", "SHORT"}:
        raise ValueError(direction)

    base: dict[str, Any] = {
        "direction": direction,
        "entry_price": entry_price,
        "measurement_origin": config.measurement_origin,
        "min_required_distance_pct": config.min_target_distance_pct,
        "min_required_distance_bps": config.min_target_distance_bps,
        "config_source_path": config.config_source_path,
        "config_loaded_at": config.config_loaded_at,
        "target_pool_id": None,
        "target_pool_timeframe": None,
        "target_edge": None,
        "target_price": None,
        "raw_target_distance_pct": None,
        "raw_target_distance_bps": None,
        "overlap_detected": False,
        "target_causally_available": False,
        "gate_passed": False,
        "gate_reason": "INVALID_ROOM_GATE_CONFIG",
    }
    base.update(_cost_room_fields(0.0, config))
    for key in list(base):
        if key.startswith("room_after_cost_"):
            base[key] = None

    if not config.enabled:
        base["gate_passed"] = True
        base["gate_reason"] = "TARGET_DISTANCE_SUFFICIENT"
        base["gate_disabled"] = True
        return base

    if not (entry_price > 0):
        base["gate_reason"] = "INVALID_ROOM_GATE_CONFIG"
        return base

    overlap = _htf_opposing_overlap(direction, entry_price, pools)
    inside_opposing = _entry_inside_opposing(direction, entry_price, pools)
    base["overlap_detected"] = overlap

    if inside_opposing:
        base["gate_reason"] = "ENTRY_INSIDE_OPPOSING_POOL"
        base["gate_passed"] = False

    if direction == "LONG":
        directional = [p for p in pools if p.side == "ASK" and p.lower_edge > entry_price]
        sort_key = lambda p: p.lower_edge  # noqa: E731
        target_price_fn = lambda p: p.lower_edge  # noqa: E731
        target_edge_label = "lower"
    else:
        directional = [p for p in pools if p.side == "BID" and p.upper_edge < entry_price]
        sort_key = lambda p: -p.upper_edge  # noqa: E731
        target_price_fn = lambda p: p.upper_edge  # noqa: E731
        target_edge_label = "upper"

    if not directional:
        if inside_opposing:
            return base
        base["gate_reason"] = "TARGET_NOT_OBSERVED"
        if config.missing_target_policy == "block":
            base["gate_passed"] = False
        return base

    causal = [p for p in directional if p.active_as_of]
    if as_of_iso is not None:
        as_of = _utc(as_of_iso)
        causal = [
            p
            for p in directional
            if p.active_as_of and _utc(p.available_at) <= as_of
        ]

    if not causal:
        base["gate_reason"] = "TARGET_NOT_CAUSALLY_AVAILABLE"
        base["target_causally_available"] = False
        if config.missing_target_policy == "block":
            base["gate_passed"] = False
        return base

    causal_sorted = sorted(causal, key=sort_key)
    target = causal_sorted[0]
    target_price = float(target_price_fn(target))
    raw_pct = _distance_pct(direction, entry_price, target_price)
    raw_bps = raw_pct * 100.0

    base.update(
        {
            "target_pool_id": target.pool_id,
            "target_pool_timeframe": target.source_timeframe,
            "target_pool_first_available_ts": target.available_at,
            "target_edge": target_edge_label,
            "target_price": target_price,
            "raw_target_distance_pct": raw_pct,
            "raw_target_distance_bps": raw_bps,
            "target_causally_available": True,
        }
    )
    base.update(_cost_room_fields(raw_bps, config))

    distance_ok = _compare_distance(raw_pct, config)
    structural_block = False
    if inside_opposing:
        base["gate_reason"] = "ENTRY_INSIDE_OPPOSING_POOL"
        structural_block = True
    elif overlap and config.overlap_policy == "block":
        base["gate_reason"] = "HTF_OPPOSING_POOL_OVERLAP"
        structural_block = True
    elif distance_ok:
        base["gate_reason"] = "TARGET_DISTANCE_SUFFICIENT"
        base["gate_passed"] = True
    else:
        base["gate_reason"] = "TARGET_DISTANCE_BELOW_MINIMUM"
        structural_block = True

    if structural_block:
        base["gate_passed"] = False

    return base
