"""Toxicity-style features and absorption/break event rows for EXECUTION walls."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Sequence

from orderbook_analyse.execution_wall_detector.types import ExecutionWallSequence
from orderbook_analyse.wall_toxicity_audit.types import SpoofingSuspicion


def absorption_event_rows(
    sequences: Sequence[ExecutionWallSequence],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for seq in sequences:
        if not seq.absorption_candidate and seq.executed_qty_estimate <= 0:
            continue
        peak = max(seq.peak_qty, 1e-9)
        rows.append(
            {
                "wall_sequence_id": seq.wall_sequence_id,
                "side": seq.side,
                "absorption_candidate": seq.absorption_candidate,
                "aggressive_trade_qty_at_wall": seq.executed_qty_estimate,
                "visible_qty_consumed": seq.executed_qty_estimate,
                "refilled_qty": seq.refilled_qty,
                "refill_count": seq.refill_count,
                "refill_ratio": seq.refilled_qty / peak,
                "executed_to_peak_ratio": seq.executed_qty_estimate / peak,
                "executed_to_visible_ratio": (
                    seq.executed_qty_estimate / max(seq.initial_qty, 1e-9)
                ),
                "absorption_duration_ms": seq.lifetime_ms if seq.absorption_candidate else None,
                "absorption_efficiency": (
                    (seq.executed_qty_estimate / peak)
                    * (1.0 + seq.refilled_qty / peak)
                    if seq.absorption_candidate
                    else None
                ),
                "wall_type": seq.wall_type,
            }
        )
    return [r for r in rows if r["absorption_candidate"] or r["aggressive_trade_qty_at_wall"] > 0]


def toxicity_rows(sequences: Sequence[ExecutionWallSequence]) -> list[dict[str, Any]]:
    """Lightweight toxicity features for execution walls (no SPOOFING_PROVEN)."""
    out: list[dict[str, Any]] = []
    for seq in sequences:
        reasons: list[str] = []
        suspicion = SpoofingSuspicion.LOW.value
        removed = seq.cancelled_or_pulled_qty_estimate + seq.unexplained_removed_qty
        total_removed = removed + seq.executed_qty_estimate
        removed_without_trade_ratio = (
            removed / total_removed if total_removed > 0 else None
        )
        pulled_before_touch = seq.pulled_before_touch
        if pulled_before_touch:
            reasons.append("pulled_before_touch")
        if removed_without_trade_ratio is not None and removed_without_trade_ratio >= 0.7:
            reasons.append("high_removed_without_trade_ratio")
        moved_away = any(
            t.get("transition_type") == "MOVED_AWAY_FROM_MARKET" for t in seq.transitions
        )
        moved_toward = any(
            t.get("transition_type") == "MOVED_TOWARD_MARKET" for t in seq.transitions
        )
        if moved_away and (seq.min_distance_bps or 0) <= 30:
            reasons.append("moved_away_as_price_approached")
        near_mig = moved_toward or moved_away
        if near_mig and (seq.min_distance_bps or 999) <= 30:
            reasons.append("near_market_migration")
        place_cancel = sum(
            1
            for t in seq.transitions
            if t.get("transition_type") in {"APPEARED", "DISAPPEARED", "SHRANK"}
        )
        if place_cancel >= 6 and seq.executed_qty_estimate <= 0:
            reasons.append("repeated_place_cancel_pattern")
        if seq.executed_qty_estimate <= 0 and (seq.min_distance_bps or 999) <= 10:
            reasons.append("large_imbalance_without_execution_proxy")

        score = len(reasons)
        if score >= 3 or (pulled_before_touch and (removed_without_trade_ratio or 0) >= 0.8):
            suspicion = SpoofingSuspicion.HIGH.value
        elif score >= 1:
            suspicion = SpoofingSuspicion.MEDIUM.value

        out.append(
            {
                "wall_sequence_id": seq.wall_sequence_id,
                "side": seq.side,
                "pulled_before_touch": pulled_before_touch,
                "pull_distance_bps": seq.min_distance_bps,
                "milliseconds_before_expected_touch": None,
                "removed_without_trade_ratio": removed_without_trade_ratio,
                "near_market_migration": near_mig and (seq.min_distance_bps or 999) <= 30,
                "moved_away_as_price_approached": "moved_away_as_price_approached" in reasons,
                "repeated_place_cancel_count": place_cancel,
                "oscillation_count": sum(
                    1
                    for t in seq.transitions
                    if t.get("transition_type")
                    in {"MOVED_TOWARD_MARKET", "MOVED_AWAY_FROM_MARKET"}
                ),
                "layering_level_count": None,
                "large_imbalance_without_execution": "large_imbalance_without_execution_proxy"
                in reasons,
                "reappeared_opposite_direction": False,
                "price_reaction_after_pull": None,
                "spoofing_suspicion": suspicion,
                "suspicion_reasons": ";".join(reasons),
                "wall_type": seq.wall_type,
                "note": "estimate only; no order identity; never SPOOFING_PROVEN",
            }
        )
    return out


def forward_outcome_rows(
    sequences: Sequence[ExecutionWallSequence],
    break_events: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flatten break events as forward outcomes."""
    by_id = {s.wall_sequence_id: s for s in sequences}
    rows: list[dict[str, Any]] = []
    for ev in break_events:
        seq = by_id.get(str(ev["wall_sequence_id"]))
        rows.append(
            {
                **ev,
                "first_seen": seq.first_seen.isoformat() if seq and seq.first_seen else None,
                "representative_price": seq.representative_price if seq else None,
                "touch_status": seq.touch_status if seq else None,
                "absorption_candidate": seq.absorption_candidate if seq else None,
            }
        )
    return rows
