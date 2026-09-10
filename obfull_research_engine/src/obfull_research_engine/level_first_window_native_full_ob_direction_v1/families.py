"""Map direction-neutral features onto reaction-relative evidence families."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..bounded_level_first_analyzer_pilot_v1.episodes import parse_utc
from ..drilldown.imbalance import SAFE_DIV_EPS
from ..timeparse import format_utc_z
from . import CONTRADICTION_FAMILIES, SUPPORT_FAMILIES
from .sides import BACK_SIDE, FRONT_SIDE, normalized_sides, price_in


def _ts(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    return parse_utc(value)


def _family(status: str, *, reasons: list[str], first_ts=None, last_ts=None, present_at_detection: bool | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "reasons": reasons,
        "first_ts": format_utc_z(first_ts) if first_ts else None,
        "last_ts": format_utc_z(last_ts) if last_ts else None,
        "present_at_detection": present_at_detection,
    }


def _walls_on_side(walls: list[dict[str, Any]], side: str, zone: tuple[float, float]) -> list[dict[str, Any]]:
    out = []
    for w in walls:
        if str(w.get("side")) != side:
            continue
        if price_in(zone, float(w["price"])):
            out.append(w)
    return out


def _refills_on_side(refills: list[dict[str, Any]], side: str, zone: tuple[float, float], types: set[str]) -> list[dict[str, Any]]:
    out = []
    for r in refills:
        if str(r.get("side")) != side:
            continue
        if r.get("refill_type") not in types:
            continue
        px = r.get("refill_price") if r.get("refill_price") is not None else r.get("original_price")
        if px is None:
            continue
        if price_in(zone, float(px)):
            out.append(r)
    return out


def evaluate_families(
    *,
    reaction_direction: str,
    spatial: dict[str, Any],
    walls: list[dict[str, Any]],
    refills: list[dict[str, Any]],
    features: dict[str, Any],
    contract: dict[str, Any],
    first_touch: datetime,
    detection: datetime,
    quality_ok: bool,
) -> dict[str, dict[str, Any]]:
    if not quality_ok:
        blocked = _family("NOT_EVALUATED", reasons=["QUALITY_FAIL_CLOSED"])
        return {name: dict(blocked) for name in SUPPORT_FAMILIES + CONTRADICTION_FAMILIES}

    sides = normalized_sides(reaction_direction)
    front, back = sides[FRONT_SIDE], sides[BACK_SIDE]
    front_zone = tuple(spatial["front_zone"])
    back_zone = tuple(spatial["back_zone"])
    blocking = set(contract["wall_statuses_blocking"])
    removed = set(contract["wall_statuses_removed"])
    defense = set(contract["wall_statuses_defense"])
    exact = set(contract["refill_types_defense"])
    sign = int(contract["imbalance_supporting_sign"][reaction_direction])
    removed_ratio = 0.85
    for item in contract.get("reused_thresholds") or []:
        if item.get("name") == "wall_removed_min_ratio":
            removed_ratio = float(item["value"])

    front_walls = _walls_on_side(walls, front, front_zone)
    back_walls = _walls_on_side(walls, back, back_zone)
    front_block = [w for w in front_walls if w.get("wall_status") in blocking]
    front_removed = [w for w in front_walls if w.get("wall_status") in removed]
    back_defense_walls = [w for w in back_walls if w.get("wall_status") in defense]
    front_exact = _refills_on_side(refills, front, front_zone, exact)
    back_exact = _refills_on_side(refills, back, back_zone, exact)

    front_liq_pre = features.get("liquidity_in_front_at_touch") or {}
    front_liq_end = features.get("liquidity_in_front_at_end") or {}
    back_liq_pre = features.get("liquidity_in_back_at_touch") or {}
    back_liq_end = features.get("liquidity_in_back_at_end") or {}
    front_pre = float((front_liq_pre.get("ask_notional") if front == "ask" else front_liq_pre.get("bid_notional")) or 0.0)
    front_end = float((front_liq_end.get("ask_notional") if front == "ask" else front_liq_end.get("bid_notional")) or 0.0)
    back_pre = float((back_liq_pre.get("bid_notional") if back == "bid" else back_liq_pre.get("ask_notional")) or 0.0)
    back_end = float((back_liq_end.get("bid_notional") if back == "bid" else back_liq_end.get("ask_notional")) or 0.0)
    front_pull = front_pre > SAFE_DIV_EPS and ((front_pre - front_end) / front_pre) >= removed_ratio
    back_pull = back_pre > SAFE_DIV_EPS and ((back_pre - back_end) / back_pre) >= removed_ratio

    imb_delta = features.get("depth_imbalance_change")
    imb_support = imb_delta is not None and (imb_delta * sign) > SAFE_DIV_EPS
    imb_contra = imb_delta is not None and (imb_delta * sign) < -SAFE_DIV_EPS

    last_valid = _ts(features.get("last_valid_ts"))
    present = bool(last_valid is not None and last_valid < detection)

    out: dict[str, dict[str, Any]] = {}

    if front_removed or front_pull:
        out["FRONT_LIQUIDITY_REMOVAL_SUPPORT"] = _family(
            "SUPPORTED",
            reasons=["FRONT_WALL_REMOVED_LIKELY"] if front_removed else ["FRONT_DEPTH_REMOVED_RATIO"],
            last_ts=last_valid,
            present_at_detection=present,
        )
    else:
        out["FRONT_LIQUIDITY_REMOVAL_SUPPORT"] = _family("NOT_OBSERVED", reasons=["NO_FRONT_REMOVAL"])

    if back_defense_walls or back_exact:
        out["BACK_LIQUIDITY_DEFENSE_SUPPORT"] = _family(
            "SUPPORTED",
            reasons=["BACK_WALL_DEFENSE"] if back_defense_walls else ["BACK_EXACT_REFILL"],
            last_ts=last_valid,
            present_at_detection=True,
        )
    else:
        out["BACK_LIQUIDITY_DEFENSE_SUPPORT"] = _family("NOT_OBSERVED", reasons=["NO_BACK_DEFENSE"])

    if imb_support:
        out["DIRECTIONAL_IMBALANCE_SUPPORT"] = _family(
            "SUPPORTED",
            reasons=["IMBALANCE_MOVED_TO_SUPPORTING_SIDE"],
            last_ts=last_valid,
            present_at_detection=present,
        )
    else:
        out["DIRECTIONAL_IMBALANCE_SUPPORT"] = _family("NOT_OBSERVED", reasons=["NO_SUPPORTING_IMBALANCE_SHIFT"])

    if front_block:
        out["NON_BLOCKING_FRONT_BOOK_SUPPORT"] = _family("NOT_OBSERVED", reasons=["FRONT_BLOCKING_WALL_PRESENT"])
        out["FRONT_WALL_CONTRADICTION"] = _family(
            "CONTRADICTED",
            reasons=["FRONT_WALL_PERSISTED_OR_REFILLED"],
            last_ts=last_valid,
            present_at_detection=True,
        )
    else:
        out["NON_BLOCKING_FRONT_BOOK_SUPPORT"] = _family(
            "SUPPORTED",
            reasons=["NO_RELEVANT_FRONT_BLOCKING_WALL"],
            last_ts=last_valid,
            present_at_detection=present,
        )
        out["FRONT_WALL_CONTRADICTION"] = _family("NOT_OBSERVED", reasons=["NO_FRONT_BLOCKING_WALL"])

    if back_pull:
        out["BACK_LIQUIDITY_PULL_CONTRADICTION"] = _family(
            "CONTRADICTED",
            reasons=["BACK_DEPTH_REMOVED_RATIO"],
            last_ts=last_valid,
            present_at_detection=present,
        )
    else:
        out["BACK_LIQUIDITY_PULL_CONTRADICTION"] = _family("NOT_OBSERVED", reasons=["NO_BACK_PULL"])

    if imb_contra:
        out["DIRECTIONAL_IMBALANCE_CONTRADICTION"] = _family(
            "CONTRADICTED",
            reasons=["IMBALANCE_MOVED_AGAINST_SUPPORTING_SIDE"],
            last_ts=last_valid,
            present_at_detection=present,
        )
    else:
        out["DIRECTIONAL_IMBALANCE_CONTRADICTION"] = _family("NOT_OBSERVED", reasons=["NO_OPPOSING_IMBALANCE_SHIFT"])

    if front_exact:
        out["OPPOSING_REFILL_CONTRADICTION"] = _family(
            "CONTRADICTED",
            reasons=["FRONT_EXACT_REFILL"],
            last_ts=last_valid,
            present_at_detection=True,
        )
    else:
        out["OPPOSING_REFILL_CONTRADICTION"] = _family("NOT_OBSERVED", reasons=["NO_FRONT_EXACT_REFILL"])

    return out


def temporal_from_families(
    families: dict[str, dict[str, Any]],
    *,
    first_touch: datetime,
    detection: datetime,
) -> dict[str, Any]:
    support_ts = [_ts(families[n]["last_ts"]) for n in SUPPORT_FAMILIES if families[n]["status"] == "SUPPORTED"]
    contra_ts = [_ts(families[n]["last_ts"]) for n in CONTRADICTION_FAMILIES if families[n]["status"] == "CONTRADICTED"]
    support_ts = [t for t in support_ts if t]
    contra_ts = [t for t in contra_ts if t]
    support_at = any(families[n].get("present_at_detection") for n in SUPPORT_FAMILIES if families[n]["status"] == "SUPPORTED")
    contra_at = any(families[n].get("present_at_detection") for n in CONTRADICTION_FAMILIES if families[n]["status"] == "CONTRADICTED")
    disappeared = False
    if support_ts and not support_at:
        disappeared = max(support_ts) < first_touch
    return {
        "first_support_ts": format_utc_z(min(support_ts)) if support_ts else None,
        "last_support_ts": format_utc_z(max(support_ts)) if support_ts else None,
        "first_contradiction_ts": format_utc_z(min(contra_ts)) if contra_ts else None,
        "last_contradiction_ts": format_utc_z(max(contra_ts)) if contra_ts else None,
        "support_present_at_detection": support_at,
        "contradiction_present_at_detection": contra_at,
        "evidence_persisted": bool(support_at or contra_at),
        "evidence_disappeared_before_detection": disappeared,
    }
