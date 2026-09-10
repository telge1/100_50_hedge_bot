"""Zone touch vs wall touch separately; detection via classify_reaction."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..bounded_level_first_analyzer_pilot_v1.reactions import classify_reaction
from ..level_first_episode1_touch_detection_independent_v1.derive import derive_wall_first_touch
from ..timeparse import format_utc_z
from .visits import build_zone_touch_from_visit


def derive_detection_for_visit(
    *,
    visit: dict[str, Any],
    zone: dict[str, Any],
    candles_1m: list[dict[str, Any]],
    price_marks: list[tuple[datetime, float]],
    window_end: datetime,
) -> dict[str, Any]:
    cluster = {
        "cluster_price_low": float(zone["zone_low"]),
        "cluster_price_high": float(zone["zone_high"]),
        "level_cluster_id": zone.get("level_cluster_id") or zone["zone_id"],
        "member_level_types": zone.get("member_level_types") or [zone.get("tpo_type")],
    }
    reaction = classify_reaction(
        episode=visit,
        cluster=cluster,
        candles_1m=candles_1m,
        price_marks=price_marks,
        lld_type="MP_ONLY",
        window_end=window_end,
    )
    det_at = reaction.get("detection_available_at")
    if det_at is None:
        return {
            "event_type": "DETECTION",
            "available": False,
            "reaction": reaction,
            "exchange_event_time": None,
            "event_available_at": None,
            "detected_at": None,
        }
    det_dt = parse_utc(det_at) if not isinstance(det_at, datetime) else det_at
    return {
        "event_type": "DETECTION",
        "available": True,
        "reaction": reaction,
        "exchange_event_time": format_utc_z(det_dt),
        "event_available_at": format_utc_z(det_dt),
        "detected_at": format_utc_z(det_dt),
        "reaction_class": reaction.get("reaction_class"),
        "reason": reaction.get("reason"),
    }


def derive_wall_touch_parameterized(
    *,
    episode_id_str: str,
    zone_touch: dict[str, Any],
    trades: list[dict[str, Any]],
    replay: dict[str, Any],
    wall_price: float,
    wall_side: str,
) -> dict[str, Any]:
    """Call frozen derive_wall_first_touch with parameterized wall (never Episode-1 defaults)."""
    return derive_wall_first_touch(
        episode_id_str=episode_id_str,
        zone_touch=zone_touch,
        trades=trades,
        replay=replay,
        wall_price=float(wall_price),
        wall_side=str(wall_side),
    )


def enrich_cluster_touches(
    *,
    attack_cluster: dict[str, Any],
    zone: dict[str, Any],
    trades: list[dict[str, Any]],
    candles_1m: list[dict[str, Any]],
    price_marks: list[tuple[datetime, float]],
    window_end: datetime,
    replay: dict[str, Any] | None,
    ask_wall: dict[str, Any] | None,
    bid_wall: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build zone_touch, optional detection, and wall touches for ask/bid candidates."""
    primary = attack_cluster["primary_visit"]
    zone_touch = build_zone_touch_from_visit(primary, zone=zone, trades=trades)
    detection = derive_detection_for_visit(
        visit=primary,
        zone=zone,
        candles_1m=candles_1m,
        price_marks=price_marks,
        window_end=window_end,
    )
    wall_touches: dict[str, Any] = {}
    errors: dict[str, str] = {}
    if replay is None:
        return {
            "zone_touch": zone_touch,
            "detection": detection,
            "wall_touches": wall_touches,
            "errors": {"replay": "missing"},
        }
    for label, cand in (("ask", ask_wall), ("bid", bid_wall)):
        if not cand:
            continue
        try:
            wt = derive_wall_touch_parameterized(
                episode_id_str=primary["episode_id"],
                zone_touch=zone_touch,
                trades=trades,
                replay=replay,
                wall_price=float(cand["wall_price"]),
                wall_side=str(cand["wall_side"]),
            )
            wall_touches[label] = wt
        except Exception as exc:  # noqa: BLE001 — fail-closed per side
            errors[label] = str(exc)
    return {
        "zone_touch": zone_touch,
        "detection": detection,
        "wall_touches": wall_touches,
        "errors": errors,
    }
