"""Typed handoff from touch/detection derivation into wall-flow/QDH."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


class HandoffError(RuntimeError):
    """Fail-closed handoff validation error."""


REQUIRED_FIELDS = (
    "episode_id",
    "symbol",
    "zone_id",
    "zone_low",
    "zone_high",
    "zone_available_at",
    "zone_first_touch_exchange_event_time",
    "zone_first_touch_event_available_at",
    "zone_trigger_record_id",
    "wall_id",
    "wall_generation_id",
    "wall_side",
    "wall_price",
    "replay_epoch",
    "wall_generation_index",
    "wall_generation_start_exchange_time",
    "wall_first_touch_exchange_event_time",
    "wall_first_touch_event_available_at",
    "wall_trigger_record_id",
    "qty_at_zone_touch",
    "detection_exchange_event_time",
    "detection_event_available_at",
    "detection_reaction_class",
    "detection_reason",
)


@dataclass(frozen=True)
class Episode1Handoff:
    """Typisierter Handoff — wall-flow stage must not invent Episode timings."""

    episode_id: str
    symbol: str
    zone_id: str
    zone_low: float
    zone_high: float
    zone_available_at: str
    zone_first_touch_exchange_event_time: str
    zone_first_touch_event_available_at: str
    zone_trigger_record_id: str
    wall_id: str
    wall_generation_id: str
    wall_side: str
    wall_price: float
    replay_epoch: int
    wall_generation_index: int
    wall_generation_start_exchange_time: str
    wall_first_touch_exchange_event_time: str
    wall_first_touch_event_available_at: str
    wall_trigger_record_id: str
    qty_at_zone_touch: float
    detection_exchange_event_time: str
    detection_event_available_at: str
    detection_reaction_class: str
    detection_reason: str
    # Detector-stage research binding only — never a wall-flow feature input.
    detector_research_visit_count_note: str
    handoff_schema_version: str = "episode1_handoff_v1"
    source_detector_package: str = "level_first_episode1_touch_detection_independent_v1"
    source_wall_flow_package: str = "level_first_episode1_wall_flow_qdh_base_v1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_handoff(raw: dict[str, Any] | Episode1Handoff | None) -> Episode1Handoff:
    if raw is None:
        raise HandoffError("missing handoff")
    if isinstance(raw, Episode1Handoff):
        data = raw.to_dict()
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        raise HandoffError(f"invalid handoff type: {type(raw)}")
    missing = [k for k in REQUIRED_FIELDS if data.get(k) in (None, "")]
    if missing:
        raise HandoffError(f"handoff missing required fields: {missing}")
    if "research_visit_count" in data and data.get("forbid_research_visit_count_in_wall_flow", True):
        # Field may appear only in note; numeric research_visit_count must not drive wall-flow.
        pass
    try:
        return Episode1Handoff(
            episode_id=str(data["episode_id"]),
            symbol=str(data["symbol"]),
            zone_id=str(data["zone_id"]),
            zone_low=float(data["zone_low"]),
            zone_high=float(data["zone_high"]),
            zone_available_at=str(data["zone_available_at"]),
            zone_first_touch_exchange_event_time=str(data["zone_first_touch_exchange_event_time"]),
            zone_first_touch_event_available_at=str(data["zone_first_touch_event_available_at"]),
            zone_trigger_record_id=str(data["zone_trigger_record_id"]),
            wall_id=str(data["wall_id"]),
            wall_generation_id=str(data["wall_generation_id"]),
            wall_side=str(data["wall_side"]),
            wall_price=float(data["wall_price"]),
            replay_epoch=int(data["replay_epoch"]),
            wall_generation_index=int(data["wall_generation_index"]),
            wall_generation_start_exchange_time=str(data["wall_generation_start_exchange_time"]),
            wall_first_touch_exchange_event_time=str(data["wall_first_touch_exchange_event_time"]),
            wall_first_touch_event_available_at=str(data["wall_first_touch_event_available_at"]),
            wall_trigger_record_id=str(data["wall_trigger_record_id"]),
            qty_at_zone_touch=float(data["qty_at_zone_touch"]),
            detection_exchange_event_time=str(data["detection_exchange_event_time"]),
            detection_event_available_at=str(data["detection_event_available_at"]),
            detection_reaction_class=str(data["detection_reaction_class"]),
            detection_reason=str(data["detection_reason"]),
            detector_research_visit_count_note=str(
                data.get("detector_research_visit_count_note")
                or "research_visit_count=3 is detector-stage Episode-1 binding only; not used in wall-flow"
            ),
            handoff_schema_version=str(data.get("handoff_schema_version") or "episode1_handoff_v1"),
            source_detector_package=str(
                data.get("source_detector_package") or "level_first_episode1_touch_detection_independent_v1"
            ),
            source_wall_flow_package=str(
                data.get("source_wall_flow_package") or "level_first_episode1_wall_flow_qdh_base_v1"
            ),
        )
    except (TypeError, ValueError) as exc:
        raise HandoffError(f"handoff field type error: {exc}") from exc


def handoff_from_derivation(derivation: dict[str, Any]) -> Episode1Handoff:
    """Build handoff from touch_detection_independent_v1 derive_all output."""
    if derivation.get("coverage_error"):
        raise HandoffError(f"derivation coverage_error: {derivation['coverage_error']}")
    z = derivation.get("zone_first_touch") or {}
    w = derivation.get("wall_first_touch") or {}
    d = derivation.get("detection") or {}
    zone = derivation.get("zone_available") or {}
    if not w:
        raise HandoffError("derivation missing wall_first_touch")
    reaction = d.get("reaction") or {}
    return validate_handoff(
        {
            "episode_id": z.get("episode_id") or derivation.get("episode_id"),
            "symbol": "BTCUSDT",
            "zone_id": z.get("zone_id") or zone.get("zone_id"),
            "zone_low": z.get("zone_low", zone.get("zone_low")),
            "zone_high": z.get("zone_high", zone.get("zone_high")),
            "zone_available_at": z.get("zone_available_at") or zone.get("zone_available_at"),
            "zone_first_touch_exchange_event_time": z.get("exchange_event_time"),
            "zone_first_touch_event_available_at": z.get("event_available_at"),
            "zone_trigger_record_id": z.get("trigger_record_id"),
            "wall_id": w.get("wall_id"),
            "wall_generation_id": w.get("wall_generation_id"),
            "wall_side": w.get("wall_side"),
            "wall_price": w.get("wall_price"),
            "replay_epoch": w.get("replay_epoch"),
            "wall_generation_index": w.get("wall_generation_index"),
            "wall_generation_start_exchange_time": w.get("generation_start_exchange_time"),
            "wall_first_touch_exchange_event_time": w.get("exchange_event_time"),
            "wall_first_touch_event_available_at": w.get("event_available_at"),
            "wall_trigger_record_id": w.get("trigger_record_id"),
            "qty_at_zone_touch": w.get("qty_at_zone_touch"),
            "detection_exchange_event_time": d.get("exchange_event_time") or d.get("detected_at"),
            "detection_event_available_at": d.get("event_available_at") or d.get("detected_at"),
            "detection_reaction_class": reaction.get("reaction_class"),
            "detection_reason": reaction.get("reason"),
            "detector_research_visit_count_note": (
                "research_visit_count=3 is exclusively an Episode-1 research binding in the "
                "detector stage and is NOT a general live candidate selector; wall-flow must ignore it."
            ),
        }
    )


def wall_flow_inputs_from_handoff(h: Episode1Handoff) -> dict[str, dict[str, Any]]:
    """Map handoff → kwargs for frozen compute_wall_flow_bundle (no ISO constants)."""
    zone_touch = {
        "event_type": "ZONE_FIRST_TOUCH",
        "episode_id": h.episode_id,
        "zone_id": h.zone_id,
        "zone_low": h.zone_low,
        "zone_high": h.zone_high,
        "zone_available_at": h.zone_available_at,
        "exchange_event_time": h.zone_first_touch_exchange_event_time,
        "event_available_at": h.zone_first_touch_event_available_at,
        "trigger_record_id": h.zone_trigger_record_id,
    }
    # Wall-flow attribution gate: wall must be visible at zone touch (proven contract).
    # Generation identity is carried separately and validated before this mapping.
    wall_observation = {
        "event_type": "WALL_OBSERVATION_AT_ZONE_TOUCH",
        "wall_id": h.wall_id,
        "wall_generation_id": h.wall_generation_id,
        "wall_side": h.wall_side,
        "wall_price": h.wall_price,
        "qty": h.qty_at_zone_touch,
        "visible": True,
        "wall_visible_at": h.zone_first_touch_exchange_event_time,
        "generation_start_exchange_time": h.wall_generation_start_exchange_time,
        "replay_epoch": h.replay_epoch,
        "zone_first_touch": h.zone_first_touch_exchange_event_time,
        "is_wall_touch": False,
        "event_available_at": h.zone_first_touch_event_available_at,
    }
    wall_touch = {
        "event_type": "WALL_FIRST_TOUCH",
        "wall_id": h.wall_id,
        "wall_generation_id": h.wall_generation_id,
        "wall_side": h.wall_side,
        "wall_price": h.wall_price,
        "exchange_event_time": h.wall_first_touch_exchange_event_time,
        "event_available_at": h.wall_first_touch_event_available_at,
        "trigger_record_id": h.wall_trigger_record_id,
        "wall_visible_at": h.wall_generation_start_exchange_time,
        "replay_epoch": h.replay_epoch,
    }
    detection = {
        "event_type": "DETECTION",
        "detected_at": h.detection_exchange_event_time,
        "exchange_event_time": h.detection_exchange_event_time,
        "event_available_at": h.detection_event_available_at,
        "reaction": {
            "reaction_class": h.detection_reaction_class,
            "reason": h.detection_reason,
        },
    }
    return {
        "zone_touch": zone_touch,
        "wall_observation": wall_observation,
        "wall_touch": wall_touch,
        "detection": detection,
    }
