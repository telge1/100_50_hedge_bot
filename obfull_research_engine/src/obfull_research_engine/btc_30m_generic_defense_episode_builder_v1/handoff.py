"""Build DefenseHandoff via validate_handoff; generic schema note (no visit_count selector)."""

from __future__ import annotations

from typing import Any

from ..level_first_episode1_detection_to_wall_flow_integration_v1.handoff import (
    Episode1Handoff,
    HandoffError,
    validate_handoff,
    wall_flow_inputs_from_handoff,
)

GENERIC_VISIT_COUNT_NOTE = (
    "research_visit_count is NOT used as a selector in the generic builder "
    "(btc_30m_generic_defense_episode_builder_v1); visit_count may appear only as a "
    "diagnostic field on visit rows and must not drive discovery or wall-flow."
)

GENERIC_HANDOFF_SCHEMA = "generic_defense_handoff_v1"
GENERIC_DETECTOR_PACKAGE = "btc_30m_generic_defense_episode_builder_v1"


def build_defense_handoff(
    *,
    symbol: str,
    zone: dict[str, Any],
    zone_touch: dict[str, Any],
    wall_touch: dict[str, Any],
    detection: dict[str, Any],
) -> Episode1Handoff:
    reaction = detection.get("reaction") or {}
    raw = {
        "episode_id": zone_touch["episode_id"],
        "symbol": symbol,
        "zone_id": zone_touch.get("zone_id") or zone["zone_id"],
        "zone_low": float(zone_touch.get("zone_low", zone["zone_low"])),
        "zone_high": float(zone_touch.get("zone_high", zone["zone_high"])),
        "zone_available_at": zone_touch.get("zone_available_at") or zone["zone_available_at"],
        "zone_first_touch_exchange_event_time": zone_touch["exchange_event_time"],
        "zone_first_touch_event_available_at": zone_touch["event_available_at"],
        "zone_trigger_record_id": zone_touch.get("trigger_record_id") or "unknown",
        "wall_id": wall_touch["wall_id"],
        "wall_generation_id": wall_touch["wall_generation_id"],
        "wall_side": wall_touch["wall_side"],
        "wall_price": float(wall_touch["wall_price"]),
        "replay_epoch": int(wall_touch["replay_epoch"]),
        "wall_generation_index": int(wall_touch["wall_generation_index"]),
        "wall_generation_start_exchange_time": wall_touch["generation_start_exchange_time"],
        "wall_first_touch_exchange_event_time": wall_touch["exchange_event_time"],
        "wall_first_touch_event_available_at": wall_touch["event_available_at"],
        "wall_trigger_record_id": wall_touch.get("trigger_record_id") or "unknown",
        "qty_at_zone_touch": float(wall_touch.get("qty_at_zone_touch") or 0.0),
        "detection_exchange_event_time": detection.get("exchange_event_time")
        or detection.get("detected_at")
        or zone_touch["exchange_event_time"],
        "detection_event_available_at": detection.get("event_available_at")
        or detection.get("detected_at")
        or zone_touch["event_available_at"],
        "detection_reaction_class": detection.get("reaction_class")
        or reaction.get("reaction_class")
        or "TOUCH_UNRESOLVED",
        "detection_reason": detection.get("reason") or reaction.get("reason") or "generic_builder",
        "detector_research_visit_count_note": GENERIC_VISIT_COUNT_NOTE,
        "handoff_schema_version": GENERIC_HANDOFF_SCHEMA,
        "source_detector_package": GENERIC_DETECTOR_PACKAGE,
    }
    return validate_handoff(raw)


__all__ = [
    "Episode1Handoff",
    "HandoffError",
    "build_defense_handoff",
    "validate_handoff",
    "wall_flow_inputs_from_handoff",
    "GENERIC_VISIT_COUNT_NOTE",
]
