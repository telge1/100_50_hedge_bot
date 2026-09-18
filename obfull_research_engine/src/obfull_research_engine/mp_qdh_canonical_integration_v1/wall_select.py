"""Deterministic defense-wall selection from reconstructed L2 (LC replay).

Ranking is size-desc, then distance-to-zone-edge asc, then price asc.
Never uses outcomes. Ties → WALL_SELECTION_UNRESOLVED.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Sequence

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.mp_ob_feature_enrichment_v1.lc_book import BookState, apply_level_change
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands
from obfull_research_engine.mp_wall_flow_qdh_silver_v1.silver_adapters import ns_to_dt

from . import TICK_SIZE


def make_wall_id(*, wall_side: str, wall_price: float, zone_id: str) -> str:
    raw = f"{wall_side}|{wall_price:.10f}|{zone_id}"
    return "w_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _snap(price: float, tick: float = TICK_SIZE) -> float:
    return round(round(float(price) / float(tick)) * float(tick), 10)


def _zone_edge_distance(price: float, *, role: str, lo: float, hi: float) -> float:
    role = role.upper()
    if role == "UPPER":
        # ask defense near high edge
        return abs(float(price) - float(hi))
    return abs(float(price) - float(lo))


def select_defense_wall(
    *,
    level_changes: Sequence[LevelChangeEvent],
    event_role: str,
    confluence_low: float,
    confluence_high: float,
    zone_id: str,
    touch_price: float | None,
    zone_touch_ns: int,
    tick_size: float = TICK_SIZE,
) -> dict[str, Any]:
    bands = build_zone_bands(role=event_role, low=confluence_low, high=confluence_high)
    side = bands.defense_side
    lo, hi = float(bands.low), float(bands.high)
    # Immediate adjacency = zone edges ± canonical QDH half-band (band_ticks*tick).
    # Do not invent wider profit-driven pads; unresolved if still empty.
    from . import BAND_TICKS

    pad = float(BAND_TICKS) * float(tick_size)
    qmin, qmax = lo - pad, hi + pad

    book = BookState()
    first_visible: dict[float, int] = {}
    ordered = sorted(
        level_changes,
        key=lambda ev: (int(ev.event_time_ns), int(ev.apply_order or 0)),
    )
    for ev in ordered:
        if int(ev.event_time_ns) > int(zone_touch_ns):
            break
        if str(ev.side).lower() != side:
            continue
        px = _snap(float(ev.price), tick_size)
        if not (qmin - 1e-12 <= px <= qmax + 1e-12):
            continue
        old, new = apply_level_change(book, ev)
        if px not in first_visible and new > 0:
            first_visible[px] = int(ev.event_time_ns)
        if new <= 0 and px in first_visible:
            # still keep first_visible history
            pass

    snap = book.snapshot()
    candidates: list[dict[str, Any]] = []
    for (s, px), qty in snap.items():
        if str(s).lower() != side or qty <= 0:
            continue
        px = _snap(float(px), tick_size)
        if not (qmin - 1e-12 <= px <= qmax + 1e-12):
            continue
        candidates.append(
            {
                "wall_side": side,
                "wall_price": px,
                "size": float(qty),
                "dist_edge": _zone_edge_distance(px, role=event_role, lo=lo, hi=hi),
                "first_visible_ns": first_visible.get(px),
            }
        )

    if not candidates:
        return {
            "ok": False,
            "blocker_reason": "WALL_SELECTION_UNRESOLVED",
            "detail": "NO_VISIBLE_DEFENSE_LEVEL_AT_ZONE_TOUCH",
            "candidates": [],
            "defense_side": side,
            "zone_lo": lo,
            "zone_hi": hi,
        }

    # Prefer existing MP→QDH wall price contract when that level is visible.
    from obfull_research_engine.ob_forschungsengine_v1.event_spec import wall_price_from_mp_event

    mp_side, mp_price = wall_price_from_mp_event(
        {
            "event_role": event_role,
            "confluence_low": confluence_low,
            "confluence_high": confluence_high,
            "touch_price": touch_price,
        },
        tick_size=tick_size,
    )
    # If touch snaps outside zone±pad, fall back to defense edge (same as empty-touch path).
    if not (qmin - 1e-12 <= float(mp_price) <= qmax + 1e-12):
        mp_price = hi if side == "ask" else lo
        mp_price = _snap(float(mp_price), tick_size)

    preferred = None
    for c in candidates:
        if str(c["wall_side"]) == str(mp_side) and abs(float(c["wall_price"]) - float(mp_price)) <= 1e-9:
            preferred = c
            break
    # Else prefer snapped touch if inside band and visible.
    if preferred is None and touch_price is not None:
        tp = _snap(float(touch_price), tick_size)
        if qmin - 1e-12 <= tp <= qmax + 1e-12:
            for c in candidates:
                if abs(c["wall_price"] - tp) <= 1e-9:
                    preferred = c
                    break

    ranked = sorted(
        candidates,
        key=lambda c: (-float(c["size"]), float(c["dist_edge"]), float(c["wall_price"])),
    )
    if preferred is not None:
        selected = preferred
        reason = "EXISTING_MP_WALL_PRICE_CONTRACT_VISIBLE_AT_ZONE_TOUCH"
        conf = "HIGH"
    else:
        selected = ranked[0]
        reason = "RANK_SIZE_DESC_THEN_EDGE_DIST_ASC_THEN_PRICE_ASC"
        conf = "MEDIUM"
        if len(ranked) >= 2:
            a, b = ranked[0], ranked[1]
            if (
                abs(float(a["size"]) - float(b["size"])) <= 1e-12
                and abs(float(a["dist_edge"]) - float(b["dist_edge"])) <= 1e-12
            ):
                return {
                    "ok": False,
                    "blocker_reason": "WALL_SELECTION_UNRESOLVED",
                    "detail": "TOP_CANDIDATES_TIED",
                    "candidates": ranked[:5],
                    "defense_side": side,
                    "zone_lo": lo,
                    "zone_hi": hi,
                }

    fv = selected.get("first_visible_ns")
    wall_id = make_wall_id(wall_side=side, wall_price=float(selected["wall_price"]), zone_id=zone_id)
    return {
        "ok": True,
        "blocker_reason": None,
        "wall_id": wall_id,
        "wall_side": side,
        "wall_price": float(selected["wall_price"]),
        "wall_size_at_zone_touch": float(selected["size"]),
        "wall_first_visible_ts_ns": fv,
        "wall_visible_at_zone_touch": True,
        "wall_rank_at_zone_touch": 1,
        "wall_selection_reason": reason,
        "wall_selection_confidence": conf,
        "candidates": ranked[:8],
        "defense_side": side,
        "zone_lo": lo,
        "zone_hi": hi,
        "legacy_enrichment_band_note": "LEGACY uses zone ±1/2/5bps depth aggregates; canonical uses wall±band_ticks",
    }
