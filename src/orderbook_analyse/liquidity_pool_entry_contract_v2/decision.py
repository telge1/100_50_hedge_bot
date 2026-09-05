"""Direction-symmetric mechanical decision resolver (Entry Contract V2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from orderbook_analyse.liquidity_pool_entry_contract_v2.geometry import PoolGeometry
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.config import (
    EffectiveRoomConfig,
)
from orderbook_analyse.liquidity_pool_min_target_distance_config_v1.gate import (
    PoolCandidate,
    evaluate_room_to_target_gate,
)


@dataclass(frozen=True)
class BranchGates:
    direction: str
    entry_price: float | None
    first_available_ts: str | None
    microstructure_gate_passed: bool
    microstructure_gate_reason: str
    room_gate: dict[str, Any]


@dataclass(frozen=True)
class MechanicalDecision:
    reaction: str
    mechanical_verdict: str
    candidate_direction: str | None
    first_available_ts: str | None
    mechanical_entry_price: float | None
    microstructure_gate_passed: bool
    microstructure_gate_reason: str
    room_gate: dict[str, Any] | None
    mechanical_trade_verdict: str
    long_branch: BranchGates
    short_branch: BranchGates
    pool_side: str
    approach: str


def geom_rows_to_pool_candidates(rows: Sequence[dict[str, Any]]) -> list[PoolCandidate]:
    return [
        PoolCandidate(
            pool_id=str(r["pool_id"]),
            source_timeframe=str(r["source_timeframe"]),
            side=str(r["side"]),
            lower_edge=float(r["lower_edge"]),
            upper_edge=float(r["upper_edge"]),
            available_at=str(r["available_at"]),
            active_as_of=True,
        )
        for r in rows
    ]


def empty_room_gate(direction: str, reason: str = "INVALID_ROOM_GATE_CONFIG") -> dict[str, Any]:
    return {
        "direction": direction,
        "entry_price": None,
        "target_pool_id": None,
        "target_pool_timeframe": None,
        "target_pool_first_available_ts": None,
        "target_edge": None,
        "target_price": None,
        "raw_target_distance_pct": None,
        "raw_target_distance_bps": None,
        "overlap_detected": False,
        "target_causally_available": False,
        "gate_passed": False,
        "gate_reason": reason,
        "room_after_cost_11bps": None,
        "room_after_cost_15bps": None,
        "room_after_cost_20bps": None,
    }


def evaluate_branch_gates(
    *,
    direction: str,
    microstructure_ok: bool,
    microstructure_reason: str,
    entry_price: float | None,
    first_available_ts: str | None,
    pools: Sequence[PoolCandidate],
    effective: EffectiveRoomConfig,
) -> BranchGates:
    direction = direction.upper()
    if entry_price is None or first_available_ts is None:
        room = empty_room_gate(direction, "TARGET_NOT_OBSERVED")
    else:
        room = evaluate_room_to_target_gate(
            direction=direction,
            entry_price=float(entry_price),
            pools=pools,
            config=effective.room,
            as_of_iso=first_available_ts,
        )
    return BranchGates(
        direction=direction,
        entry_price=entry_price,
        first_available_ts=first_available_ts,
        microstructure_gate_passed=microstructure_ok,
        microstructure_gate_reason=microstructure_reason,
        room_gate=room,
    )


def branch_gates_to_dict(branch: BranchGates) -> dict[str, Any]:
    return {
        "direction": branch.direction,
        "entry_price": branch.entry_price,
        "first_available_ts": branch.first_available_ts,
        "microstructure_gate_passed": branch.microstructure_gate_passed,
        "microstructure_gate_reason": branch.microstructure_gate_reason,
        "room_gate": branch.room_gate,
    }


def _labels(geom: PoolGeometry) -> dict[str, str]:
    """Verdict / reaction labels for defense vs breakout by pool side."""
    if geom.pool_side == "BID":
        return {
            "defense_reaction": "BID_DEFENSE_ABSORPTION_RECLAIM",
            "defense_verdict": "CLEAR_BID_DEFENSE_LONG_CANDIDATE",
            "breakout_reaction": "BID_BACK_EDGE_BREAK_ACCEPTED",
            "breakout_verdict": "CLEAR_BID_BREAKOUT_SHORT_CANDIDATE",
        }
    return {
        "defense_reaction": "ASK_DEFENSE_ABSORPTION_REJECTION",
        "defense_verdict": "CLEAR_ASK_DEFENSE_SHORT_CANDIDATE",
        "breakout_reaction": "ASK_BACK_EDGE_BREAK_ACCEPTED",
        "breakout_verdict": "CLEAR_ASK_BREAKOUT_LONG_CANDIDATE",
    }


@dataclass(frozen=True)
class MicroEvidence:
    """Side-agnostic microstructure evidence at a pool contact.

    defense_ok: defense/reclaim/rejection confirmed for geom.defense_trade_direction
    breakout_ok: accepted back-edge break for geom.breakout_trade_direction
    breakout_contested: break then reclaim / ambiguous break acceptance
    attack_eff_count / counter_count / two_sided_count: aggressor tallies
    """

    seen_inside: bool
    arrival_present: bool
    defense_ok: bool
    breakout_ok: bool
    breakout_contested: bool
    defense_entry: float | None
    breakout_entry: float | None
    defense_first_ts: str | None
    breakout_first_ts: str | None
    attack_eff_count: int = 0
    counter_count: int = 0
    two_sided_count: int = 0


def resolve_mechanical_decision(
    *,
    geom: PoolGeometry,
    evidence: MicroEvidence,
    pools: Sequence[PoolCandidate],
    effective: EffectiveRoomConfig,
) -> MechanicalDecision:
    """Resolve TRADE/NO_TRADE using identical thresholds for ASK and BID."""
    labels = _labels(geom)
    defense_dir = geom.defense_trade_direction
    breakout_dir = geom.breakout_trade_direction

    # Map to LONG/SHORT branch slots (always both evaluated)
    long_ok = evidence.defense_ok if defense_dir == "LONG" else evidence.breakout_ok
    short_ok = evidence.defense_ok if defense_dir == "SHORT" else evidence.breakout_ok
    # Contested applies to breakout branch only (V1: short_contested on BID breakout)
    long_contested = evidence.breakout_contested if breakout_dir == "LONG" else False
    short_contested = evidence.breakout_contested if breakout_dir == "SHORT" else False

    long_entry = (
        evidence.defense_entry if defense_dir == "LONG" else evidence.breakout_entry
    )
    short_entry = (
        evidence.defense_entry if defense_dir == "SHORT" else evidence.breakout_entry
    )
    long_first = (
        evidence.defense_first_ts if defense_dir == "LONG" else evidence.breakout_first_ts
    )
    short_first = (
        evidence.defense_first_ts if defense_dir == "SHORT" else evidence.breakout_first_ts
    )

    long_micro_reason = (
        "MICROSTRUCTURE_CONFIRMED" if long_ok and not long_contested else (
            "AMBIGUOUS_POOL_CONTEST" if long_contested else "NO_CLEAR_MICROSTRUCTURE_CONFIRMATION"
        )
    )
    if short_contested:
        short_micro_reason = "AMBIGUOUS_POOL_CONTEST"
    elif short_ok:
        short_micro_reason = "MICROSTRUCTURE_CONFIRMED"
    else:
        short_micro_reason = "NO_CLEAR_MICROSTRUCTURE_CONFIRMATION"

    long_branch = evaluate_branch_gates(
        direction="LONG",
        microstructure_ok=long_ok and not long_contested,
        microstructure_reason=long_micro_reason,
        entry_price=long_entry,
        first_available_ts=long_first,
        pools=pools,
        effective=effective,
    )
    short_branch = evaluate_branch_gates(
        direction="SHORT",
        microstructure_ok=short_ok and not short_contested,
        microstructure_reason=short_micro_reason,
        entry_price=short_entry,
        first_available_ts=short_first,
        pools=pools,
        effective=effective,
    )

    reaction = "NO_CAUSAL_POOL_REACTION"
    mechanical_verdict = "NO_CAUSAL_POOL_REACTION"
    candidate_direction: str | None = None
    first_available_ts: str | None = None
    mechanical_entry_price: float | None = None
    micro_passed = False
    micro_reason = "NO_CLEAR_MICROSTRUCTURE_CONFIRMATION"
    room_gate: dict[str, Any] | None = None
    mechanical_trade_verdict = "NO_TRADE"

    breakout_contested = evidence.breakout_contested
    defense_ok = evidence.defense_ok
    breakout_ok = evidence.breakout_ok

    if not evidence.seen_inside and not evidence.arrival_present:
        mechanical_verdict = "NO_CAUSAL_POOL_REACTION"
        reaction = "NO_CAUSAL_POOL_REACTION"
        micro_reason = "NO_CAUSAL_POOL_REACTION"
    elif breakout_contested and defense_ok:
        mechanical_verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reaction = "TWO_SIDED_CONTEST_WITH_FAILED_BREAK_ACCEPTANCE"
        micro_reason = "AMBIGUOUS_POOL_CONTEST"
    elif breakout_ok and defense_ok:
        mechanical_verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reaction = "BOTH_BRANCHES_EVIDENCE"
        micro_reason = "AMBIGUOUS_POOL_CONTEST"
    elif breakout_contested and not defense_ok:
        mechanical_verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reaction = "BREAK_THEN_RECLAIM_CONTEST"
        candidate_direction = breakout_dir
        first_available_ts = evidence.breakout_first_ts
        mechanical_entry_price = evidence.breakout_entry
        micro_reason = "AMBIGUOUS_POOL_CONTEST"
        room_gate = short_branch.room_gate if breakout_dir == "SHORT" else long_branch.room_gate
    elif breakout_ok:
        reaction = labels["breakout_reaction"]
        candidate_direction = breakout_dir
        first_available_ts = evidence.breakout_first_ts
        mechanical_entry_price = evidence.breakout_entry
        branch = short_branch if breakout_dir == "SHORT" else long_branch
        micro_passed = branch.microstructure_gate_passed
        micro_reason = branch.microstructure_gate_reason
        room_gate = branch.room_gate
        mechanical_verdict = labels["breakout_verdict"]
        if micro_passed and room_gate.get("gate_passed"):
            mechanical_trade_verdict = f"TRADE_{breakout_dir}_CANDIDATE"
    elif defense_ok:
        reaction = labels["defense_reaction"]
        candidate_direction = defense_dir
        first_available_ts = evidence.defense_first_ts
        mechanical_entry_price = evidence.defense_entry
        branch = long_branch if defense_dir == "LONG" else short_branch
        micro_passed = branch.microstructure_gate_passed
        micro_reason = branch.microstructure_gate_reason
        room_gate = branch.room_gate
        mechanical_verdict = labels["defense_verdict"]
        if micro_passed and room_gate.get("gate_passed"):
            mechanical_trade_verdict = f"TRADE_{defense_dir}_CANDIDATE"
    elif evidence.two_sided_count >= 5 or (
        evidence.attack_eff_count and evidence.counter_count
    ):
        mechanical_verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reaction = "TWO_SIDED_CONTEST"
        micro_reason = "AMBIGUOUS_POOL_CONTEST"
    else:
        mechanical_verdict = "AMBIGUOUS_POOL_CONTEST_NO_TRADE"
        reaction = "UNCLEAR_POOL_REACTION"
        micro_reason = "AMBIGUOUS_POOL_CONTEST"

    return MechanicalDecision(
        reaction=reaction,
        mechanical_verdict=mechanical_verdict,
        candidate_direction=candidate_direction,
        first_available_ts=first_available_ts,
        mechanical_entry_price=mechanical_entry_price,
        microstructure_gate_passed=micro_passed,
        microstructure_gate_reason=micro_reason,
        room_gate=room_gate,
        mechanical_trade_verdict=mechanical_trade_verdict,
        long_branch=long_branch,
        short_branch=short_branch,
        pool_side=geom.pool_side,
        approach=geom.approach,
    )


def flatten_room_gate_for_mech(
    effective: EffectiveRoomConfig,
    decision: MechanicalDecision,
) -> dict[str, Any]:
    room = decision.room_gate or empty_room_gate(
        decision.candidate_direction or "LONG", "TARGET_NOT_OBSERVED"
    )
    return {
        "microstructure_gate_passed": decision.microstructure_gate_passed,
        "microstructure_gate_reason": decision.microstructure_gate_reason,
        "candidate_direction": decision.candidate_direction,
        "mechanical_entry_price": decision.mechanical_entry_price,
        "room_gate_enabled": effective.room.enabled,
        "room_gate_config_path": effective.config_path_rel,
        "room_gate_config_sha256": effective.config_sha256,
        "min_target_distance_pct": effective.room.min_target_distance_pct,
        "min_target_distance_bps": effective.room.min_target_distance_bps,
        "target_pool_id": room.get("target_pool_id"),
        "target_pool_first_available_ts": room.get("target_pool_first_available_ts"),
        "target_edge": room.get("target_edge"),
        "target_price": room.get("target_price"),
        "raw_target_distance_pct": room.get("raw_target_distance_pct"),
        "raw_target_distance_bps": room.get("raw_target_distance_bps"),
        "room_after_cost_11bps": room.get("room_after_cost_11bps"),
        "room_after_cost_15bps": room.get("room_after_cost_15bps"),
        "room_after_cost_20bps": room.get("room_after_cost_20bps"),
        "overlap_detected": room.get("overlap_detected"),
        "target_causally_available": room.get("target_causally_available"),
        "room_gate_passed": room.get("gate_passed"),
        "room_gate_reason": room.get("gate_reason"),
        "mechanical_trade_verdict": decision.mechanical_trade_verdict,
    }


def prefix_parity(
    *,
    decision: MechanicalDecision,
    pools: Sequence[PoolCandidate],
    effective: EffectiveRoomConfig,
    geom: PoolGeometry,
    case_pool_id: str,
) -> dict[str, Any]:
    """ASK/BID prefix parity over decision-critical fields."""
    base = {
        "pool_id": case_pool_id,
        "front_edge": geom.front_edge,
        "back_edge": geom.back_edge,
        "reaction": decision.reaction,
        "candidate_direction": decision.candidate_direction,
        "first_available_ts": decision.first_available_ts,
        "mechanical_entry_price": decision.mechanical_entry_price,
        "mechanical_verdict": decision.mechanical_verdict,
        "mechanical_trade_verdict": decision.mechanical_trade_verdict,
        "microstructure_gate_passed": decision.microstructure_gate_passed,
        "microstructure_gate_reason": decision.microstructure_gate_reason,
        "room_gate_config_sha256": effective.config_sha256,
        "entry_contract_version": "liquidity_pool_entry_contract/v2",
        "pool_side": geom.pool_side,
        "approach": geom.approach,
    }
    if decision.candidate_direction is None or decision.mechanical_entry_price is None:
        return {
            **base,
            "checked": False,
            "reason": "no_candidate",
            "prefix_status": "EXACT_PREFIX_PARITY",
        }
    prefix_room = evaluate_room_to_target_gate(
        direction=decision.candidate_direction,
        entry_price=float(decision.mechanical_entry_price),
        pools=pools,
        config=effective.room,
        as_of_iso=decision.first_available_ts,
    )
    full_room = decision.room_gate or {}
    keys = (
        "target_pool_id",
        "target_edge",
        "target_price",
        "target_pool_first_available_ts",
        "raw_target_distance_bps",
        "gate_passed",
        "gate_reason",
        "target_causally_available",
    )
    mismatches = [k for k in keys if prefix_room.get(k) != full_room.get(k)]
    entry_full = decision.microstructure_gate_passed and bool(full_room.get("gate_passed"))
    entry_prefix = decision.microstructure_gate_passed and bool(prefix_room.get("gate_passed"))
    if entry_full != entry_prefix:
        mismatches.append("entry_eligible")
    status = "EXACT_PREFIX_PARITY" if not mismatches else "PREFIX_PARITY_FAILURE"
    return {
        **base,
        "checked": True,
        "prefix_status": status,
        "mismatches": mismatches,
        "full_room_gate": {k: full_room.get(k) for k in keys},
        "prefix_room_gate": {k: prefix_room.get(k) for k in keys},
        "raw_target_distance_bps_full": full_room.get("raw_target_distance_bps"),
        "raw_target_distance_bps_prefix": prefix_room.get("raw_target_distance_bps"),
        "gate_reason_full": full_room.get("gate_reason"),
        "gate_reason_prefix": prefix_room.get("gate_reason"),
    }
