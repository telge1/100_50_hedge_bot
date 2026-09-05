"""A+ quality gates (research-only)."""

from __future__ import annotations

from typing import Any

from orderbook_analyse.l2_wall_attack_discovery.models import tick_size

from .config import MIN_GROSS_RR, MIN_NET_REWARD_DISTANCE_TICKS, VERIFIED_TICK_SYMBOLS
from .models import GateResult, PoolRecord, ScannerCandidate


REQUIRED_GATES = (
    "pool_known_before_approach",
    "closed_bar_safe",
    "timeframe_context_complete",
    "clear_entry_pool",
    "clear_target_pool",
    "no_major_intermediate_pool",
    "confirmed_1m_reaction_wick_break",
    "structural_stop_available",
    "verified_tick_size",
    "sufficient_candle_coverage",
    "no_data_gap",
    "unique_episode",
    "target_not_already_reached",
    "entry_before_target",
    "stop_on_correct_side",
    "minimum_net_reward_distance_after_costs",
)


def gross_rr(direction: str, entry: float, stop: float, target: float) -> float | None:
    if direction == "LONG":
        risk = entry - stop
        reward = target - entry
    else:
        risk = stop - entry
        reward = entry - target
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def estimated_net_rr(gross: float | None, *, cost_bps: float = 15.0) -> float | None:
    if gross is None:
        return None
    # research estimate only — not execution PnL
    return gross - (2 * cost_bps / 10000.0)


def evaluate_gates(
    cand: ScannerCandidate,
    *,
    symbol: str,
    approach_at_known: bool,
    closed_bar_safe: bool,
    context_complete: bool,
    intermediate_block: bool,
    confirmed_1m: bool,
    limit_filled: bool = False,
    candle_coverage_ok: bool,
    no_data_gap: bool,
    unique_episode: bool,
    target_reached_before_entry: bool,
    tick_verified: bool | None = None,
) -> list[GateResult]:
    tick_ok = tick_verified if tick_verified is not None else symbol.upper() in VERIFIED_TICK_SYMBOLS
    entry = cand.entry_price
    stop = cand.stop_price
    target = cand.target_price
    direction = cand.direction

    stop_side_ok = False
    if entry is not None and stop is not None and target is not None:
        if direction == "LONG":
            stop_side_ok = stop < entry < target
        else:
            stop_side_ok = target < entry < stop

    grr = gross_rr(direction, float(entry or 0), float(stop or 0), float(target or 0)) if entry and stop and target else None
    net = estimated_net_rr(grr)
    tick = tick_size(symbol) if tick_ok else None
    net_dist_ok = False
    if entry is not None and target is not None and tick and tick > 0:
        net_dist_ok = abs(target - entry) / tick >= MIN_NET_REWARD_DISTANCE_TICKS
    rr_ok = grr is not None and grr >= MIN_GROSS_RR and net_dist_ok

    is_pullback = "PULLBACK" in cand.setup_type
    confirm_gate = limit_filled if is_pullback else confirmed_1m

    gates = [
        GateResult("pool_known_before_approach", approach_at_known),
        GateResult("closed_bar_safe", closed_bar_safe),
        GateResult("timeframe_context_complete", context_complete),
        GateResult("clear_entry_pool", cand.entry_pool is not None),
        GateResult("clear_target_pool", cand.target_pool is not None),
        GateResult("no_major_intermediate_pool", not intermediate_block),
        GateResult(
            "confirmed_1m_reaction_wick_break" if not is_pullback else "pullback_limit_filled",
            confirm_gate,
        ),
        GateResult("structural_stop_available", stop is not None),
        GateResult("verified_tick_size", tick_ok, None if tick_ok else "TICK_SIZE_UNVERIFIED"),
        GateResult("sufficient_candle_coverage", candle_coverage_ok),
        GateResult("no_data_gap", no_data_gap),
        GateResult("unique_episode", unique_episode),
        GateResult("target_not_already_reached", not target_reached_before_entry),
        GateResult("entry_before_target", stop_side_ok),
        GateResult("stop_on_correct_side", stop_side_ok),
        GateResult("minimum_net_reward_distance_after_costs", rr_ok),
    ]
    return gates


def apply_gates(cand: ScannerCandidate, gates: list[GateResult]) -> None:
    cand.gates = gates
    failed = [g for g in gates if not g.passed]
    cand.reason_codes = [g.reason or g.gate for g in failed if g.reason]
    if failed:
        cand.reason_codes.extend(g.gate for g in failed if not g.reason)
