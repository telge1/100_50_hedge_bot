"""Causal pre-touch wall candidate tracking (zone±5 ticks; no post-touch selection)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from obfull_research_engine.breakout_xray_v1.ports import LevelChangeEvent
from obfull_research_engine.mp_ob_feature_enrichment_v1.lc_book import BookState, apply_level_change
from obfull_research_engine.mp_ob_feature_enrichment_v1.zones import build_zone_bands
from obfull_research_engine.mp_qdh_canonical_integration_v1.wall_select import make_wall_id
from obfull_research_engine.ob_forschungsengine_v1.event_spec import wall_price_from_mp_event

from . import BAND_TICKS, TICK_SIZE


def _snap(price: float, tick: float = TICK_SIZE) -> float:
    return round(round(float(price) / float(tick)) * float(tick), 10)


@dataclass
class PriceTrack:
    price: float
    first_seen_ns: int | None = None
    last_seen_ns: int | None = None
    max_queue: float = 0.0
    queue_at_touch: float = 0.0
    n_add: int = 0
    n_update: int = 0
    n_delete: int = 0
    cum_increase: float = 0.0
    cum_decrease: float = 0.0
    last_positive_ns: int | None = None
    ever_positive: bool = False
    # for move detection: when this price went to 0, where did size appear nearby
    depleted_ns: int | None = None


@dataclass
class LinkageResult:
    status: str
    selected: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    defense_side: str
    zone_lo: float
    zone_hi: float
    qmin: float
    qmax: float
    detail: str = ""


def track_pre_touch_walls(
    *,
    level_changes: Sequence[LevelChangeEvent],
    event_role: str,
    confluence_low: float,
    confluence_high: float,
    zone_id: str,
    touch_price: float | None,
    zone_touch_ns: int,
    pre_touch_start_ns: int,
    tick_size: float = TICK_SIZE,
    band_ticks: int = BAND_TICKS,
    eligible_trade_qty_by_price: dict[float, float] | None = None,
    attributed_fill_by_price: dict[float, float] | None = None,
) -> LinkageResult:
    """Track defense-side levels in zone±band_ticks from pre_touch_start through touch.

    Never uses LC after zone_touch_ns for selection/classification.
    """
    bands = build_zone_bands(role=event_role, low=confluence_low, high=confluence_high)
    side = bands.defense_side
    lo, hi = float(bands.low), float(bands.high)
    pad = float(band_ticks) * float(tick_size)
    qmin, qmax = lo - pad, hi + pad

    tracks: dict[float, PriceTrack] = {}
    book = BookState()
    ordered = sorted(
        level_changes,
        key=lambda ev: (int(ev.event_time_ns), int(ev.apply_order or 0)),
    )

    # Seed: apply LCs before pre_touch_start so queue at window open is known,
    # but first_seen only counts visibility inside the audit window.
    for ev in ordered:
        ts = int(ev.event_time_ns)
        if ts > int(zone_touch_ns):
            break
        if str(ev.side).lower() != side:
            continue
        px = _snap(float(ev.price), tick_size)
        if not (qmin - 1e-12 <= px <= qmax + 1e-12):
            continue
        old, new = apply_level_change(book, ev)
        tr = tracks.get(px)
        if tr is None:
            tr = PriceTrack(price=px)
            tracks[px] = tr
        ct = str(ev.change_type).upper()
        if ct in ("ADD", "INSERT"):
            tr.n_add += 1
        elif ct in ("DELETE", "REMOVE"):
            tr.n_delete += 1
        else:
            tr.n_update += 1
        if new > old:
            tr.cum_increase += new - old
        elif old > new:
            tr.cum_decrease += old - new
        if ts >= int(pre_touch_start_ns):
            if new > 0 and tr.first_seen_ns is None:
                tr.first_seen_ns = ts
            if new > 0:
                tr.ever_positive = True
                tr.last_positive_ns = ts
                tr.last_seen_ns = ts
                tr.max_queue = max(tr.max_queue, float(new))
            if old > 0 and new <= 0:
                tr.depleted_ns = ts
                tr.last_seen_ns = ts

    # Queue at touch from snapshot
    snap = book.snapshot()
    for (s, px), qty in snap.items():
        if str(s).lower() != side:
            continue
        px = _snap(float(px), tick_size)
        if px not in tracks:
            tracks[px] = PriceTrack(price=px, ever_positive=qty > 0, queue_at_touch=float(qty), max_queue=float(qty))
            if qty > 0:
                tracks[px].first_seen_ns = int(zone_touch_ns)
                tracks[px].last_seen_ns = int(zone_touch_ns)
                tracks[px].last_positive_ns = int(zone_touch_ns)
        else:
            tracks[px].queue_at_touch = float(qty)
            if qty > 0:
                tracks[px].max_queue = max(tracks[px].max_queue, float(qty))
                tracks[px].ever_positive = True

    eligible = eligible_trade_qty_by_price or {}
    fills = attributed_fill_by_price or {}

    candidates: list[dict[str, Any]] = []
    for px, tr in tracks.items():
        if not tr.ever_positive and tr.queue_at_touch <= 0:
            continue
        dist_edge = abs(px - hi) if side == "ask" else abs(px - lo)
        mid_ref = float(touch_price) if touch_price is not None else (lo + hi) / 2.0
        dist_mid = abs(px - mid_ref) / float(tick_size)
        visible_s = 0.0
        if tr.first_seen_ns is not None and tr.last_positive_ns is not None:
            visible_s = max(0.0, (int(tr.last_positive_ns) - int(tr.first_seen_ns)) / 1e9)
        wall_id = make_wall_id(wall_side=side, wall_price=px, zone_id=zone_id)
        candidates.append(
            {
                "candidate_wall_id": wall_id,
                "wall_side": side,
                "wall_price_first": px,
                "wall_price_last": px,
                "first_seen_ns": tr.first_seen_ns,
                "last_seen_ns": tr.last_seen_ns,
                "last_positive_ns": tr.last_positive_ns,
                "depleted_ns": tr.depleted_ns,
                "max_queue": tr.max_queue,
                "queue_at_touch": tr.queue_at_touch,
                "visible_duration_seconds": visible_s,
                "distance_to_mp_edge_ticks": dist_edge / float(tick_size),
                "distance_to_touch_mid_ticks": dist_mid,
                "n_add": tr.n_add,
                "n_update": tr.n_update,
                "n_delete": tr.n_delete,
                "cumulative_increase": tr.cum_increase,
                "cumulative_decrease": tr.cum_decrease,
                "eligible_public_trade_qty_band_proxy": float(eligible.get(px, 0.0)),
                "attributed_fill_proxy": float(fills.get(px, 0.0)),
                "present_at_touch": tr.queue_at_touch > 1e-12,
            }
        )

    if not candidates:
        return LinkageResult(
            status="NO_CANONICAL_WALL",
            selected=None,
            candidates=[],
            defense_side=side,
            zone_lo=lo,
            zone_hi=hi,
            qmin=qmin,
            qmax=qmax,
            detail="NO_POSITIVE_DEFENSE_LEVEL_IN_PRE_TOUCH_WINDOW_OR_AT_TOUCH",
        )

    # Selection ranking (documented, outcome-blind) among present-at-touch first;
    # historical selection for depleted/pulled uses same rank among ever-positive.
    present = [c for c in candidates if c["present_at_touch"]]
    historical = [c for c in candidates if float(c["max_queue"]) > 0]

    def rank_key(c: dict[str, Any]) -> tuple:
        # 1 correct side (already filtered)
        # 2 inside zone/tolerance (already)
        # 3 longest visibility
        # 4 highest max queue
        # 5 smallest edge distance
        # 6 price asc / wall_id
        return (
            -float(c["visible_duration_seconds"]),
            -float(c["max_queue"]),
            float(c["distance_to_mp_edge_ticks"]),
            float(c["wall_price_first"]),
            str(c["candidate_wall_id"]),
        )

    # Prefer MP contract price if present at touch
    mp_side, mp_price = wall_price_from_mp_event(
        {
            "event_role": event_role,
            "confluence_low": confluence_low,
            "confluence_high": confluence_high,
            "touch_price": touch_price,
        },
        tick_size=tick_size,
    )
    if not (qmin - 1e-12 <= float(mp_price) <= qmax + 1e-12):
        mp_price = hi if side == "ask" else lo
        mp_price = _snap(float(mp_price), tick_size)

    if present:
        ranked = sorted(present, key=rank_key)
        preferred = None
        for c in ranked:
            if abs(float(c["wall_price_first"]) - float(mp_price)) <= 1e-9 and str(mp_side) == side:
                preferred = c
                break
        if preferred is None:
            if len(ranked) >= 2 and rank_key(ranked[0])[:3] == rank_key(ranked[1])[:3]:
                # stricter: first 3 rank components equal → ambiguous
                a, b = ranked[0], ranked[1]
                if (
                    abs(float(a["visible_duration_seconds"]) - float(b["visible_duration_seconds"])) < 1e-9
                    and abs(float(a["max_queue"]) - float(b["max_queue"])) < 1e-12
                    and abs(float(a["distance_to_mp_edge_ticks"]) - float(b["distance_to_mp_edge_ticks"])) < 1e-9
                ):
                    return LinkageResult(
                        status="AMBIGUOUS_MULTIPLE_WALLS",
                        selected=None,
                        candidates=ranked,
                        defense_side=side,
                        zone_lo=lo,
                        zone_hi=hi,
                        qmin=qmin,
                        qmax=qmax,
                        detail="TOP_PRESENT_CANDIDATES_TIED",
                    )
            selected = ranked[0]
            selected = {**selected, "selection_reason": "RANK_VISIBILITY_QUEUE_EDGE_PRICE"}
        else:
            selected = {**preferred, "selection_reason": "MP_WALL_PRICE_CONTRACT_PRESENT_AT_TOUCH"}
        selected["rejection_reason"] = None
        for c in ranked:
            if c["candidate_wall_id"] != selected["candidate_wall_id"]:
                c["rejection_reason"] = "NOT_SELECTED_LOWER_RANK"
        return LinkageResult(
            status="PRESENT_AT_TOUCH",
            selected=selected,
            candidates=sorted(candidates, key=rank_key),
            defense_side=side,
            zone_lo=lo,
            zone_hi=hi,
            qmin=qmin,
            qmax=qmax,
            detail="DEFENSE_LEVEL_POSITIVE_AT_TOUCH",
        )

    # No present-at-touch → classify historical
    ranked_h = sorted(historical, key=rank_key)
    if not ranked_h:
        return LinkageResult(
            status="NO_CANONICAL_WALL",
            selected=None,
            candidates=candidates,
            defense_side=side,
            zone_lo=lo,
            zone_hi=hi,
            qmin=qmin,
            qmax=qmax,
            detail="NO_HISTORICAL_POSITIVE_LEVEL",
        )
    if len(ranked_h) >= 2:
        a, b = ranked_h[0], ranked_h[1]
        if (
            abs(float(a["visible_duration_seconds"]) - float(b["visible_duration_seconds"])) < 1e-9
            and abs(float(a["max_queue"]) - float(b["max_queue"])) < 1e-12
            and abs(float(a["distance_to_mp_edge_ticks"]) - float(b["distance_to_mp_edge_ticks"])) < 1e-9
        ):
            return LinkageResult(
                status="AMBIGUOUS_MULTIPLE_WALLS",
                selected=None,
                candidates=ranked_h,
                defense_side=side,
                zone_lo=lo,
                zone_hi=hi,
                qmin=qmin,
                qmax=qmax,
                detail="TOP_HISTORICAL_CANDIDATES_TIED",
            )

    top = ranked_h[0]
    fill = float(top.get("attributed_fill_proxy") or 0.0)
    decrease = float(top.get("cumulative_decrease") or 0.0)
    # Move: depleted while another price in band gained size near same time
    moved = False
    if top.get("depleted_ns") is not None:
        dep_ns = int(top["depleted_ns"])
        for other in ranked_h[1:]:
            if other.get("first_seen_ns") is None:
                continue
            # appeared within 1s of depletion and has increase
            if abs(int(other["first_seen_ns"]) - dep_ns) <= 1_000_000_000 and float(other["cumulative_increase"]) > 0:
                moved = True
                break
            # or other was already present and increased around depletion
            if float(other["cumulative_increase"]) > 0 and other.get("last_seen_ns"):
                if abs(int(other["last_seen_ns"]) - dep_ns) <= 1_000_000_000:
                    moved = True
                    break

    if moved:
        status = "MOVED_BEFORE_TOUCH"
        detail = "LEVEL_EMPTIED_WITH_NEARBY_BAND_INCREASE"
    elif decrease > 1e-12 and fill >= 0.5 * decrease:
        status = "DEPLETED_BEFORE_TOUCH"
        detail = "FILL_DOMINATES_CUMULATIVE_DECREASE_BEFORE_TOUCH"
    elif decrease > 1e-12:
        status = "PULLED_BEFORE_TOUCH"
        detail = "DECREASE_WITHOUT_DOMINANT_FILL_BEFORE_TOUCH"
    else:
        status = "NO_CANONICAL_WALL"
        detail = "HISTORICAL_TRACK_WITHOUT_CLEAR_DEPLETION"
        top = None

    if top is not None:
        top = {**top, "selection_reason": f"HISTORICAL_{status}", "rejection_reason": None}
        for c in ranked_h:
            if top is None or c["candidate_wall_id"] != top["candidate_wall_id"]:
                c["rejection_reason"] = c.get("rejection_reason") or "NOT_SELECTED_LOWER_RANK"

    return LinkageResult(
        status=status,
        selected=top,
        candidates=sorted(candidates, key=rank_key),
        defense_side=side,
        zone_lo=lo,
        zone_hi=hi,
        qmin=qmin,
        qmax=qmax,
        detail=detail,
    )
