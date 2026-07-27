"""Phase 4: segment-wise wall observations, lifecycle, and timeline join.

Reuses analyze_resolution / WallDetectorParams. No long/short signals.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Mapping, Sequence

from orderbook_analyse.dynamic_wall_detector import (
    WallCluster,
    WallDetectorParams,
    _ensure_aware,
    analyze_resolution,
    choose_bucket_size,
    infer_tick_size,
)
from orderbook_analyse.orderbook_replay import (
    BookLevelEvent,
    OrderBookReplayer,
    OrderBookState,
    ReplayError,
    group_messages,
)
from orderbook_analyse.replay_segmentation import ReplayGap, ReplaySegment
from orderbook_analyse.segment_replay import load_segment_events, sample_grid

logger = logging.getLogger(__name__)

DEFAULT_WALL_RESOLUTIONS_BPS = (5.0, 10.0, 20.0, 50.0)
PREFERRED_RESOLUTION_BPS = 10.0
PREFERRED_RESOLUTION_NAME = "auto_10bps"

PHASE4_OUTPUT_FILES = (
    "wall_observations.csv",
    "wall_candidates_history.csv",
    "wall_clusters_history.csv",
    "wall_sequences.csv",
    "wall_transitions.csv",
    "wall_segment_summary.csv",
    "wall_history_errors.csv",
)


@dataclass
class WallHistoryParams:
    sample_interval_sec: int = 60
    warmup_seconds: int = 300
    resolutions_bps: tuple[float, ...] = DEFAULT_WALL_RESOLUTIONS_BPS
    preferred_bps: float = PREFERRED_RESOLUTION_BPS
    distance_max_pct: float = 5.0
    wall_multiple_min: float = 3.0
    percentile_min: float = 90.0
    depth_share_min: float = 0.01
    local_radius: int = 5
    cluster_max_gap_buckets: int = 1
    match_distance_bps: float = 10.0
    test_distance_bps: float = 5.0
    break_distance_bps: float = 5.0
    min_age_seconds: float = 60.0
    notional_change_threshold_pct: float = 20.0
    output_mode: str = "candidates"  # candidates | all_buckets
    stale_sample_intervals: float = 2.0

    def detector_params(self) -> WallDetectorParams:
        return WallDetectorParams(
            wall_multiple_min=self.wall_multiple_min,
            percentile_min=self.percentile_min,
            depth_share_min=self.depth_share_min,
            local_radius=self.local_radius,
            distance_max_pct=self.distance_max_pct,
            cluster_max_gap_buckets=self.cluster_max_gap_buckets,
        )


def parse_wall_resolutions(raw: str | Sequence[float] | None) -> list[float]:
    if raw is None or raw == "":
        return list(DEFAULT_WALL_RESOLUTIONS_BPS)
    if isinstance(raw, str):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        vals = [float(p) for p in parts]
    else:
        vals = [float(x) for x in raw]
    if not vals or any(v <= 0 for v in vals):
        raise ValueError("wall resolutions must be positive numbers")
    out: list[float] = []
    for v in vals:
        if v not in out:
            out.append(v)
    return out


def resolution_name(bps: float) -> str:
    if float(bps) == float(int(bps)):
        return f"auto_{int(bps)}bps"
    return f"auto_{bps}bps"


def wall_sample_times(
    segment_start: datetime,
    segment_end: datetime,
    *,
    interval_seconds: int,
    warmup_seconds: int,
) -> list[datetime]:
    """Deterministic samples at start+n*interval, only after warmup and <= end."""
    start = _ensure_aware(segment_start)
    end = _ensure_aware(segment_end)
    if interval_seconds <= 0:
        return []
    feature_start = start + timedelta(seconds=max(int(warmup_seconds), 0))
    if feature_start >= end:
        return []
    grid = sample_grid(start, end, interval_seconds=interval_seconds)
    return [t for t in grid if t >= feature_start and t <= end]


def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return f


def _fmt(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return format(v, "f")
    return str(v)


def _pct_change(prev: Decimal, cur: Decimal) -> float | None:
    if prev == 0:
        return None
    return _safe_float((cur - prev) / prev * Decimal("100"))


def _bps_distance(price: Decimal, mid: Decimal) -> float | None:
    if mid <= 0:
        return None
    return _safe_float(abs(price - mid) / mid * Decimal("10000"))


def _side_depth(book: OrderBookState, side: str) -> Decimal:
    levels = book.bids if side == "bid" else book.asks
    total = Decimal("0")
    for p, q in levels.items():
        total += p * q
    return total


def infer_tick_from_book(book: OrderBookState) -> Decimal:
    prices = list(book.bids.keys()) + list(book.asks.keys())
    try:
        return infer_tick_size(prices, fallback=Decimal("0.0001"))
    except ValueError:
        return Decimal("0.0001")


def observe_book_walls(
    book: OrderBookState,
    *,
    symbol: str,
    segment_id: str,
    sample_ts: datetime,
    params: WallHistoryParams,
    observation_id_start: int = 1,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Extract compact wall observation + cluster rows from current book (no clone kept)."""
    mid = book.mid_price()
    bb, ba = book.best_bid(), book.best_ask()
    if mid is None or mid <= 0 or bb is None or ba is None:
        return [], [], observation_id_start
    tick = infer_tick_from_book(book)
    det = params.detector_params()
    spread = ba - bb
    spread_bps = _safe_float(spread / mid * Decimal("10000"))
    bid_depth = _side_depth(book, "bid")
    ask_depth = _side_depth(book, "ask")
    observations: list[dict[str, Any]] = []
    clusters_out: list[dict[str, Any]] = []
    oid = observation_id_start

    # Map bucket -> cluster id per resolution/side for assignment
    for bps in params.resolutions_bps:
        res_name = resolution_name(bps)
        bucket_size = choose_bucket_size(mid, tick, bps)
        analysis = analyze_resolution(
            book, bucket_size=bucket_size, resolution=res_name, mid=mid, params=det
        )
        walls: list = analysis["walls"]
        clusters: list[WallCluster] = analysis["clusters"]
        candidates: list = analysis["candidates"] if params.output_mode == "all_buckets" else walls

        # cluster lookup by price membership
        cluster_by_price: dict[Decimal, tuple[int, WallCluster]] = {}
        for ci, cl in enumerate(clusters, start=1):
            cid = f"{res_name}:{cl.side}:C{ci:04d}"
            for w in walls:
                if w.side != cl.side:
                    continue
                if cl.start_price <= w.bucket_price <= cl.end_price:
                    cluster_by_price[w.bucket_price] = (ci, cl)
            dist_min_bps = _safe_float(Decimal(str(cl.distance_min_pct)) * Decimal("100"))
            dist_max_bps = _safe_float(Decimal(str(cl.distance_max_pct)) * Decimal("100"))
            side_walls = [w for w in walls if w.side == cl.side and cl.start_price <= w.bucket_price <= cl.end_price]
            clusters_out.append(
                {
                    "symbol": symbol,
                    "segment_id": segment_id,
                    "sample_ts": _ensure_aware(sample_ts).isoformat(),
                    "resolution": res_name,
                    "cluster_id": cid,
                    "side": cl.side,
                    "start_price": _fmt(cl.start_price),
                    "end_price": _fmt(cl.end_price),
                    "strongest_bucket_price": _fmt(cl.strongest_bucket),
                    "strongest_bucket_notional": _fmt(cl.strongest_bucket_notional),
                    "total_quantity": _fmt(cl.total_qty),
                    "total_notional": _fmt(cl.total_notional),
                    "bucket_count": cl.bucket_count,
                    "distance_min_bps": dist_min_bps,
                    "distance_max_bps": dist_max_bps,
                    "wall_multiple_max": max((w.wall_multiple for w in side_walls), default=None),
                    "percentile_max": max((w.percentile for w in side_walls), default=None),
                    "depth_share_total": _safe_float(float(cl.total_notional) / float(bid_depth + ask_depth)) if (bid_depth + ask_depth) > 0 else None,
                }
            )

        # rank by notional on side among walls
        for side in ("bid", "ask"):
            side_items = sorted(
                [c for c in candidates if c.side == side and (params.output_mode == "all_buckets" or c.is_wall)],
                key=lambda x: x.notional,
                reverse=True,
            )
            for rank, c in enumerate(side_items, start=1):
                side_depth = bid_depth if side == "bid" else ask_depth
                dist_bps = _bps_distance(c.bucket_price, mid)
                cl_info = cluster_by_price.get(c.bucket_price)
                cl_id = None
                cl_start = cl_end = cl_notional = None
                cl_count = None
                if cl_info is not None:
                    ci, cl = cl_info
                    cl_id = f"{res_name}:{cl.side}:C{ci:04d}"
                    cl_start, cl_end = _fmt(cl.start_price), _fmt(cl.end_price)
                    cl_notional = _fmt(cl.total_notional)
                    cl_count = cl.bucket_count
                obs_id = f"{segment_id}:{res_name}:{side}:O{oid:06d}"
                oid += 1
                observations.append(
                    {
                        "wall_observation_id": obs_id,
                        "symbol": symbol,
                        "segment_id": segment_id,
                        "sample_ts": _ensure_aware(sample_ts).isoformat(),
                        "resolution": res_name,
                        "bucket_size": _fmt(bucket_size),
                        "side": side,
                        "wall_price": _fmt(c.bucket_price),
                        "wall_quantity": _fmt(c.qty),
                        "wall_notional": _fmt(c.notional),
                        "wall_multiple": _safe_float(c.wall_multiple),
                        "percentile": _safe_float(c.percentile),
                        "depth_share": _safe_float(c.depth_share),
                        "distance_to_mid_abs": _fmt(abs(c.bucket_price - mid)),
                        "distance_to_mid_pct": _safe_float(c.distance_pct),
                        "distance_to_mid_bps": dist_bps,
                        "local_median_notional": _safe_float(c.local_median_notional),
                        "local_mean_notional": None,
                        "rank_on_side": rank,
                        "is_wall": bool(c.is_wall),
                        "cluster_id": cl_id,
                        "cluster_start_price": cl_start,
                        "cluster_end_price": cl_end,
                        "cluster_total_notional": cl_notional,
                        "cluster_bucket_count": cl_count,
                        "best_bid": _fmt(bb),
                        "best_ask": _fmt(ba),
                        "mid_price": _fmt(mid),
                        "spread": _fmt(spread),
                        "spread_bps": spread_bps,
                        "active_bid_levels": len(book.bids),
                        "active_ask_levels": len(book.asks),
                        "total_bid_depth_notional": _fmt(bid_depth),
                        "total_ask_depth_notional": _fmt(ask_depth),
                        "side_depth_notional": _fmt(side_depth),
                        "wall_share_of_side_depth": _safe_float(float(c.notional) / float(side_depth)) if side_depth > 0 else None,
                        "feature_emission_allowed": True,
                        "source_update_id": book.last_update_id,
                        "source_cross_sequence": book.last_seq,
                        "_price_dec": c.bucket_price,
                        "_notional_dec": c.notional,
                        "_mid_dec": mid,
                    }
                )
    return observations, clusters_out, oid


def candidates_history_from_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    preferred_resolution: str = PREFERRED_RESOLUTION_NAME,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_sample: dict[str, list[Mapping[str, Any]]] = {}
    for o in observations:
        if o.get("resolution") != preferred_resolution or not o.get("is_wall"):
            continue
        by_sample.setdefault(str(o.get("sample_ts")), []).append(o)
    for sample_ts, items in by_sample.items():
        bid = [x for x in items if x.get("side") == "bid"]
        ask = [x for x in items if x.get("side") == "ask"]
        nearest_bid = min(bid, key=lambda x: x.get("distance_to_mid_bps") or 1e18) if bid else None
        nearest_ask = min(ask, key=lambda x: x.get("distance_to_mid_bps") or 1e18) if ask else None
        strongest_bid = max(bid, key=lambda x: Decimal(str(x.get("wall_notional") or 0))) if bid else None
        strongest_ask = max(ask, key=lambda x: Decimal(str(x.get("wall_notional") or 0))) if ask else None
        for o in items:
            rows.append(
                {
                    "symbol": o.get("symbol"),
                    "segment_id": o.get("segment_id"),
                    "sample_ts": o.get("sample_ts"),
                    "wall_observation_id": o.get("wall_observation_id"),
                    "side": o.get("side"),
                    "wall_price": o.get("wall_price"),
                    "wall_notional": o.get("wall_notional"),
                    "wall_multiple": o.get("wall_multiple"),
                    "percentile": o.get("percentile"),
                    "depth_share": o.get("depth_share"),
                    "distance_bps": o.get("distance_to_mid_bps"),
                    "cluster_id": o.get("cluster_id"),
                    "is_near_wall": o is nearest_bid or o is nearest_ask,
                    "is_dominant_wall": o is strongest_bid or o is strongest_ask,
                    "is_strongest_side_wall": (
                        (o.get("side") == "bid" and o is strongest_bid)
                        or (o.get("side") == "ask" and o is strongest_ask)
                    ),
                    "best_bid": o.get("best_bid"),
                    "best_ask": o.get("best_ask"),
                    "mid_price": o.get("mid_price"),
                }
            )
    rows.sort(key=lambda r: (str(r.get("sample_ts")), str(r.get("side")), str(r.get("wall_price"))))
    return rows


def match_observations(
    previous: Sequence[Mapping[str, Any]],
    current: Sequence[Mapping[str, Any]],
    *,
    match_distance_bps: float,
) -> list[tuple[Mapping[str, Any], Mapping[str, Any], float]]:
    """Unique greedy match: same side+resolution, price within match_distance_bps."""
    if not previous or not current:
        return []
    candidates: list[tuple[float, float, float, int, int]] = []
    for i, prev in enumerate(previous):
        for j, cur in enumerate(current):
            if prev.get("side") != cur.get("side"):
                continue
            if prev.get("resolution") != cur.get("resolution"):
                continue
            pp = prev.get("_price_dec")
            cp = cur.get("_price_dec")
            mid = cur.get("_mid_dec") or prev.get("_mid_dec")
            if pp is None or cp is None or mid is None or mid <= 0:
                continue
            dist_bps = float(abs(cp - pp) / mid * Decimal("10000"))
            if dist_bps > match_distance_bps:
                continue
            # cluster overlap bonus
            overlap = 0.0
            if prev.get("cluster_id") and prev.get("cluster_id") == cur.get("cluster_id"):
                overlap = 1.0
            pn = float(prev.get("_notional_dec") or prev.get("wall_notional") or 1.0) or 1.0
            cn = float(cur.get("_notional_dec") or cur.get("wall_notional") or 0.0)
            notional_diff = abs(cn - pn) / pn
            # sort key: higher better → negate distance, prefer overlap, prefer similar notional
            candidates.append((-dist_bps, overlap, -notional_diff, i, j))
    candidates.sort(reverse=True)
    used_p: set[int] = set()
    used_c: set[int] = set()
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any], float]] = []
    for neg_dist, _ov, _nd, i, j in candidates:
        if i in used_p or j in used_c:
            continue
        used_p.add(i)
        used_c.add(j)
        matches.append((previous[i], current[j], -neg_dist))
    return matches


@dataclass
class _ActiveWall:
    sequence_id: str
    side: str
    resolution: str
    first_seen_ts: datetime
    last_seen_ts: datetime
    sample_count: int
    first_price: Decimal
    last_price: Decimal
    min_price: Decimal
    max_price: Decimal
    first_notional: Decimal
    last_notional: Decimal
    min_notional: Decimal
    max_notional: Decimal
    max_wall_multiple: float
    max_percentile: float
    max_depth_share: float
    min_distance_bps: float
    max_distance_bps: float
    was_near_price: bool = False
    was_tested: bool = False
    was_broken: bool = False
    touched: bool = False
    traded_through: bool = False
    confirmed_broken: bool = False
    disappeared_before_test: bool = False
    end_reason: str = "ACTIVE_AT_SEGMENT_END"
    closed_ts: datetime | None = None
    last_obs: Mapping[str, Any] = field(default_factory=dict)
    # diagnostics
    min_test_distance_bps: float | None = None
    first_test_ts: datetime | None = None
    first_traded_through_ts: datetime | None = None
    confirmed_break_ts: datetime | None = None
    test_price_source: str = "segment_replay_mid_high_low"


def _iso_to_dt(v: Any) -> datetime:
    if isinstance(v, datetime):
        return _ensure_aware(v)
    return _ensure_aware(datetime.fromisoformat(str(v)))


def _finalize_sequence_row(aw: _ActiveWall) -> dict[str, Any]:
    age = (aw.last_seen_ts - aw.first_seen_ts).total_seconds()
    return {
        "symbol": aw.sequence_id.split(":", 1)[0],
        "segment_id": aw.sequence_id.split(":")[1] if ":" in aw.sequence_id else None,
        "wall_sequence_id": aw.sequence_id,
        "side": aw.side,
        "resolution": aw.resolution,
        "first_seen_ts": aw.first_seen_ts.isoformat(),
        "last_seen_ts": aw.last_seen_ts.isoformat(),
        "closed_ts": None if aw.closed_ts is None else aw.closed_ts.isoformat(),
        "age_seconds": age,
        "sample_count": aw.sample_count,
        "first_price": _fmt(aw.first_price),
        "last_price": _fmt(aw.last_price),
        "min_price": _fmt(aw.min_price),
        "max_price": _fmt(aw.max_price),
        "price_move_abs": _fmt(aw.last_price - aw.first_price),
        "price_move_bps": _safe_float(
            (aw.last_price - aw.first_price)
            / ((aw.first_price + aw.last_price) / Decimal("2"))
            * Decimal("10000")
        )
        if (aw.first_price + aw.last_price) != 0
        else None,
        "first_notional": _fmt(aw.first_notional),
        "last_notional": _fmt(aw.last_notional),
        "min_notional": _fmt(aw.min_notional),
        "max_notional": _fmt(aw.max_notional),
        "notional_change_abs": _fmt(aw.last_notional - aw.first_notional),
        "notional_change_pct": _pct_change(aw.first_notional, aw.last_notional),
        "max_wall_multiple": _safe_float(aw.max_wall_multiple),
        "max_percentile": _safe_float(aw.max_percentile),
        "max_depth_share": _safe_float(aw.max_depth_share),
        "min_distance_bps": _safe_float(aw.min_distance_bps),
        "max_distance_bps": _safe_float(aw.max_distance_bps),
        "was_near_price": aw.was_near_price,
        "was_tested": aw.was_tested,
        "was_broken": aw.was_broken,
        "touched": aw.touched,
        "traded_through": aw.traded_through,
        "confirmed_broken": aw.confirmed_broken,
        "disappeared_before_test": bool(
            aw.end_reason == "DISAPPEARED" and not aw.was_tested
        ),
        "ended_by_segment": aw.end_reason in {"SEGMENT_END", "ACTIVE_AT_SEGMENT_END"},
        "end_reason": aw.end_reason,
        "min_test_distance_bps": aw.min_test_distance_bps,
        "first_test_ts": None if aw.first_test_ts is None else aw.first_test_ts.isoformat(),
        "first_traded_through_ts": None
        if aw.first_traded_through_ts is None
        else aw.first_traded_through_ts.isoformat(),
        "confirmed_break_ts": None
        if aw.confirmed_break_ts is None
        else aw.confirmed_break_ts.isoformat(),
        "test_price_source": aw.test_price_source,
    }


def build_sequences_and_transitions(
    observations: Sequence[Mapping[str, Any]],
    *,
    symbol: str,
    segment_id: str,
    segment_end_ts: datetime,
    params: WallHistoryParams,
    preferred_only: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Track preferred-resolution walls across samples into sequences + transitions.

    Final sequences are exported once via ``final_by_id`` (idempotent map).
    """
    preferred = resolution_name(params.preferred_bps)
    by_ts: dict[datetime, list[Mapping[str, Any]]] = {}
    for o in observations:
        if preferred_only and o.get("resolution") != preferred:
            continue
        if not o.get("is_wall"):
            continue
        ts = _iso_to_dt(o["sample_ts"])
        by_ts.setdefault(ts, []).append(o)
    times = sorted(by_ts.keys())
    transitions: list[dict[str, Any]] = []
    seq_counter = 1
    thr = Decimal(str(params.notional_change_threshold_pct)) / Decimal("100")
    near_bps = params.test_distance_bps
    final_by_id: dict[str, _ActiveWall] = {}

    def _new_seq(obs: Mapping[str, Any], ts: datetime) -> _ActiveWall:
        nonlocal seq_counter
        side = str(obs["side"]).upper()
        sid = f"{symbol}:{segment_id}:{side}:W{seq_counter:06d}"
        seq_counter += 1
        price = Decimal(str(obs["_price_dec"]))
        notion = Decimal(str(obs["_notional_dec"]))
        dist = float(obs.get("distance_to_mid_bps") or 0.0)
        aw = _ActiveWall(
            sequence_id=sid,
            side=str(obs["side"]),
            resolution=str(obs["resolution"]),
            first_seen_ts=ts,
            last_seen_ts=ts,
            sample_count=1,
            first_price=price,
            last_price=price,
            min_price=price,
            max_price=price,
            first_notional=notion,
            last_notional=notion,
            min_notional=notion,
            max_notional=notion,
            max_wall_multiple=float(obs.get("wall_multiple") or 0.0),
            max_percentile=float(obs.get("percentile") or 0.0),
            max_depth_share=float(obs.get("depth_share") or 0.0),
            min_distance_bps=dist,
            max_distance_bps=dist,
            was_near_price=dist <= near_bps,
            last_obs=obs,
        )
        transitions.append(
            {
                "symbol": symbol,
                "segment_id": segment_id,
                "wall_sequence_id": sid,
                "transition_ts": ts.isoformat(),
                "side": aw.side,
                "resolution": aw.resolution,
                "transition_type": "APPEARED",
                "previous_price": None,
                "current_price": _fmt(price),
                "price_change_bps": None,
                "previous_notional": None,
                "current_notional": _fmt(notion),
                "notional_change_abs": None,
                "notional_change_pct": None,
                "previous_distance_bps": None,
                "current_distance_bps": dist,
                "sample_gap_seconds": None,
                "details": "first sighting",
            }
        )
        return aw

    def _emit_transition(
        aw: _ActiveWall,
        ttype: str,
        prev: Mapping[str, Any],
        cur: Mapping[str, Any],
        ts: datetime,
        gap: float | None,
        details: str = "",
    ) -> None:
        pp = Decimal(str(prev["_price_dec"]))
        cp = Decimal(str(cur["_price_dec"]))
        pn = Decimal(str(prev["_notional_dec"]))
        cn = Decimal(str(cur["_notional_dec"]))
        mid = Decimal(str(cur.get("_mid_dec") or prev.get("_mid_dec") or 0))
        pcb = _safe_float((cp - pp) / mid * Decimal("10000")) if mid > 0 else None
        transitions.append(
            {
                "symbol": symbol,
                "segment_id": segment_id,
                "wall_sequence_id": aw.sequence_id,
                "transition_ts": ts.isoformat(),
                "side": aw.side,
                "resolution": aw.resolution,
                "transition_type": ttype,
                "previous_price": _fmt(pp),
                "current_price": _fmt(cp),
                "price_change_bps": pcb,
                "previous_notional": _fmt(pn),
                "current_notional": _fmt(cn),
                "notional_change_abs": _fmt(cn - pn),
                "notional_change_pct": _pct_change(pn, cn),
                "previous_distance_bps": prev.get("distance_to_mid_bps"),
                "current_distance_bps": cur.get("distance_to_mid_bps"),
                "sample_gap_seconds": gap,
                "details": details,
            }
        )

    def _close(aw: _ActiveWall, *, end_reason: str, closed_ts: datetime) -> None:
        if aw.sequence_id in final_by_id:
            # Already finalized — never rematerialize with a conflicting end.
            existing = final_by_id[aw.sequence_id]
            if existing.end_reason != end_reason and end_reason == "SEGMENT_END":
                return
            if existing is not aw:
                raise RuntimeError(
                    f"conflicting finalize for {aw.sequence_id}: "
                    f"{existing.end_reason} vs {end_reason}"
                )
            return
        aw.end_reason = end_reason
        aw.closed_ts = closed_ts
        if end_reason == "DISAPPEARED":
            aw.disappeared_before_test = not aw.was_tested
        final_by_id[aw.sequence_id] = aw

    prev_active: list[_ActiveWall] = []
    for ti, ts in enumerate(times):
        cur_obs = by_ts[ts]
        prev_obs = [a.last_obs for a in prev_active]
        matches = match_observations(
            prev_obs, cur_obs, match_distance_bps=params.match_distance_bps
        )
        matched_cur_ids = {id(m[1]) for m in matches}
        # Track matched ActiveWall objects before last_obs mutation
        matched_aws: set[int] = set()
        next_active: list[_ActiveWall] = []
        gap = None if ti == 0 else (ts - times[ti - 1]).total_seconds()
        obs_to_aw = {id(a.last_obs): a for a in prev_active}

        for prev, cur, _dist in matches:
            aw = obs_to_aw[id(prev)]
            matched_aws.add(id(aw))
            pp = Decimal(str(prev["_price_dec"]))
            cp = Decimal(str(cur["_price_dec"]))
            pn = Decimal(str(prev["_notional_dec"]))
            cn = Decimal(str(cur["_notional_dec"]))
            prev_dist = float(prev.get("distance_to_mid_bps") or 0.0)
            cur_dist = float(cur.get("distance_to_mid_bps") or 0.0)
            types: list[str] = ["PERSISTED"]
            if cn >= pn * (Decimal("1") + thr):
                types.append("GREW")
            elif cn <= pn * (Decimal("1") - thr):
                types.append("SHRANK")
            if cp > pp:
                types.append("MOVED_UP")
            elif cp < pp:
                types.append("MOVED_DOWN")
            if cur_dist < prev_dist - 1e-9:
                types.append("MOVED_TOWARD_PRICE")
            elif cur_dist > prev_dist + 1e-9:
                types.append("MOVED_AWAY_FROM_PRICE")
            was_near = prev_dist <= near_bps
            is_near = cur_dist <= near_bps
            if is_near and not was_near:
                types.append("BECAME_NEAR")
            if was_near and not is_near:
                types.append("LEFT_NEAR_ZONE")
            for ttype in types:
                _emit_transition(aw, ttype, prev, cur, ts, gap)
            aw.last_seen_ts = ts
            aw.sample_count += 1
            aw.last_price = cp
            aw.min_price = min(aw.min_price, cp)
            aw.max_price = max(aw.max_price, cp)
            aw.last_notional = cn
            aw.min_notional = min(aw.min_notional, cn)
            aw.max_notional = max(aw.max_notional, cn)
            aw.max_wall_multiple = max(aw.max_wall_multiple, float(cur.get("wall_multiple") or 0.0))
            aw.max_percentile = max(aw.max_percentile, float(cur.get("percentile") or 0.0))
            aw.max_depth_share = max(aw.max_depth_share, float(cur.get("depth_share") or 0.0))
            aw.min_distance_bps = min(aw.min_distance_bps, cur_dist)
            aw.max_distance_bps = max(aw.max_distance_bps, cur_dist)
            aw.was_near_price = aw.was_near_price or is_near
            aw.last_obs = cur
            if isinstance(cur, dict):
                cur["wall_sequence_id"] = aw.sequence_id
            next_active.append(aw)

        # unmatched previous → tentative DISAPPEARED (may upgrade to BROKEN after price-path)
        for aw in prev_active:
            if id(aw) in matched_aws:
                continue
            transitions.append(
                {
                    "symbol": symbol,
                    "segment_id": segment_id,
                    "wall_sequence_id": aw.sequence_id,
                    "transition_ts": ts.isoformat(),
                    "side": aw.side,
                    "resolution": aw.resolution,
                    "transition_type": "DISAPPEARED",
                    "previous_price": _fmt(aw.last_price),
                    "current_price": None,
                    "price_change_bps": None,
                    "previous_notional": _fmt(aw.last_notional),
                    "current_notional": None,
                    "notional_change_abs": None,
                    "notional_change_pct": None,
                    "previous_distance_bps": aw.last_obs.get("distance_to_mid_bps"),
                    "current_distance_bps": None,
                    "sample_gap_seconds": gap,
                    "details": "no match in current sample",
                }
            )
            _close(aw, end_reason="DISAPPEARED", closed_ts=ts)

        for cur in cur_obs:
            if id(cur) in matched_cur_ids:
                continue
            aw = _new_seq(cur, ts)
            if isinstance(cur, dict):
                cur["wall_sequence_id"] = aw.sequence_id
            next_active.append(aw)

        prev_active = next_active

    end = _ensure_aware(segment_end_ts)
    for aw in prev_active:
        transitions.append(
            {
                "symbol": symbol,
                "segment_id": segment_id,
                "wall_sequence_id": aw.sequence_id,
                "transition_ts": end.isoformat(),
                "side": aw.side,
                "resolution": aw.resolution,
                "transition_type": "SEGMENT_ENDED",
                "previous_price": _fmt(aw.last_price),
                "current_price": _fmt(aw.last_price),
                "price_change_bps": 0.0,
                "previous_notional": _fmt(aw.last_notional),
                "current_notional": _fmt(aw.last_notional),
                "notional_change_abs": "0",
                "notional_change_pct": 0.0,
                "previous_distance_bps": aw.last_obs.get("distance_to_mid_bps"),
                "current_distance_bps": aw.last_obs.get("distance_to_mid_bps"),
                "sample_gap_seconds": None,
                "details": "active at segment end",
            }
        )
        _close(aw, end_reason="SEGMENT_END", closed_ts=end)

    # Idempotent export from map — exactly one row per sequence id
    sequences = [_finalize_sequence_row(aw) for aw in final_by_id.values()]
    sequences.sort(key=lambda r: (str(r["first_seen_ts"]), str(r["wall_sequence_id"])))
    # Patch symbol/segment_id explicitly (safer than parsing id)
    for row in sequences:
        row["symbol"] = symbol
        row["segment_id"] = segment_id
    return sequences, transitions


def ask_test_distance_bps(*, wall_price: Decimal, interval_high: Decimal) -> float | None:
    """Positive while high is below ask wall; <= threshold ⇒ tested; negative ⇒ through."""
    if wall_price <= 0:
        return None
    return _safe_float((wall_price - interval_high) / wall_price * Decimal("10000"))


def bid_test_distance_bps(*, wall_price: Decimal, interval_low: Decimal) -> float | None:
    """Positive while low is above bid wall; <= threshold ⇒ tested; negative ⇒ through."""
    if wall_price <= 0:
        return None
    return _safe_float((interval_low - wall_price) / wall_price * Decimal("10000"))


def apply_test_break_flags(
    sequences: list[dict[str, Any]],
    transitions: list[dict[str, Any]],
    *,
    sample_mids: Sequence[tuple[datetime, Decimal, Decimal | None, Decimal | None]],
    params: WallHistoryParams,
) -> None:
    """Update sequences in-place using causal mid/high/low path between samples.

    Price path source: ``segment_replay_mid_high_low``.
    Each ``sample_mids`` entry ``(sample_ts, mid, hi, lo)`` is the interval ending
    at ``sample_ts`` (extrema since previous wall sample). No look-ahead past the
    sequence close interval.

    Ask test distance:
      (wall_price - interval_high) / wall_price * 10000
    Bid test distance:
      (interval_low - wall_price) / wall_price * 10000
    Tested when distance <= wall_test_distance_bps (or negative).
    """
    if not sample_mids:
        return
    path = [(_ensure_aware(t), mid, hi, lo) for t, mid, hi, lo in sample_mids]
    path.sort(key=lambda x: x[0])
    test_bps = float(params.test_distance_bps)
    break_bps = float(params.break_distance_bps)

    for s in sequences:
        side = s["side"]
        first = _iso_to_dt(s["first_seen_ts"])
        closed = (
            _iso_to_dt(s["closed_ts"])
            if s.get("closed_ts")
            else _iso_to_dt(s["last_seen_ts"])
        )
        # Primary test level: last observed wall price (bucket)
        wall_price = Decimal(str(s["last_price"]))
        if wall_price <= 0:
            continue
        break_pad = wall_price * Decimal(str(break_bps)) / Decimal("10000")
        tested = broken = touched = traded = confirmed = False
        min_test_dist: float | None = None
        first_test_ts: datetime | None = None
        first_traded_ts: datetime | None = None
        confirmed_ts: datetime | None = None

        for t, mid, hi, lo in path:
            # Intervals while wall visible, plus disappearance interval (closed_ts)
            if t < first:
                continue
            if t > closed:
                continue
            h = hi if hi is not None else mid
            l = lo if lo is not None else mid
            if side == "ask":
                dist = ask_test_distance_bps(wall_price=wall_price, interval_high=h)
                if dist is not None:
                    min_test_dist = dist if min_test_dist is None else min(min_test_dist, dist)
                    if dist <= test_bps:
                        touched = True
                        tested = True
                        if first_test_ts is None:
                            first_test_ts = t
                if h > wall_price + break_pad:
                    traded = True
                    if first_traded_ts is None:
                        first_traded_ts = t
                # confirmed break uses causal interval close (mid), no look-ahead
                if mid > wall_price + break_pad:
                    confirmed = True
                    broken = True
                    if confirmed_ts is None:
                        confirmed_ts = t
            else:
                dist = bid_test_distance_bps(wall_price=wall_price, interval_low=l)
                if dist is not None:
                    min_test_dist = dist if min_test_dist is None else min(min_test_dist, dist)
                    if dist <= test_bps:
                        touched = True
                        tested = True
                        if first_test_ts is None:
                            first_test_ts = t
                if l < wall_price - break_pad:
                    traded = True
                    if first_traded_ts is None:
                        first_traded_ts = t
                if mid < wall_price - break_pad:
                    confirmed = True
                    broken = True
                    if confirmed_ts is None:
                        confirmed_ts = t

        # traded_through alone does not set was_broken; confirmed close does
        s["was_tested"] = bool(tested)
        s["touched"] = bool(touched)
        s["traded_through"] = bool(traded)
        s["confirmed_broken"] = bool(confirmed)
        s["was_broken"] = bool(confirmed)
        s["min_test_distance_bps"] = min_test_dist
        s["first_test_ts"] = None if first_test_ts is None else first_test_ts.isoformat()
        s["first_traded_through_ts"] = (
            None if first_traded_ts is None else first_traded_ts.isoformat()
        )
        s["confirmed_break_ts"] = (
            None if confirmed_ts is None else confirmed_ts.isoformat()
        )
        s["test_price_source"] = "segment_replay_mid_high_low"

        if confirmed and s.get("end_reason") == "DISAPPEARED":
            # Break in disappearance interval supersedes DISAPPEARED end
            s["end_reason"] = "BROKEN"
            sid = s["wall_sequence_id"]
            # drop tentative DISAPPEARED rows for this sequence
            keep = [
                t
                for t in transitions
                if not (
                    t.get("wall_sequence_id") == sid
                    and t.get("transition_type") == "DISAPPEARED"
                )
            ]
            transitions.clear()
            transitions.extend(keep)

        if s.get("end_reason") == "DISAPPEARED" and not s["was_tested"]:
            s["disappeared_before_test"] = True
        else:
            s["disappeared_before_test"] = False

        def _emit(ttype: str, ts_iso: str | None, details: str) -> None:
            transitions.append(
                {
                    "symbol": s["symbol"],
                    "segment_id": s["segment_id"],
                    "wall_sequence_id": s["wall_sequence_id"],
                    "transition_ts": ts_iso or s["last_seen_ts"],
                    "side": side,
                    "resolution": s["resolution"],
                    "transition_type": ttype,
                    "previous_price": s["first_price"],
                    "current_price": s["last_price"],
                    "price_change_bps": None,
                    "previous_notional": s["first_notional"],
                    "current_notional": s["last_notional"],
                    "notional_change_abs": None,
                    "notional_change_pct": None,
                    "previous_distance_bps": s.get("max_distance_bps"),
                    "current_distance_bps": s.get("min_distance_bps"),
                    "sample_gap_seconds": None,
                    "details": details,
                }
            )

        # Deterministic order at same timestamp: TESTED → TRADED_THROUGH → BROKEN
        if tested:
            _emit("TESTED", s.get("first_test_ts"), "price_path_source=segment_replay_mid_high_low")
        if traded:
            _emit(
                "TRADED_THROUGH",
                s.get("first_traded_through_ts") or s.get("first_test_ts"),
                "interval extreme beyond break buffer",
            )
        if confirmed:
            _emit("BROKEN", s.get("confirmed_break_ts"), "confirmed_broken")
            if s.get("end_reason") not in {"SEGMENT_END", "ACTIVE_AT_SEGMENT_END"}:
                s["end_reason"] = "BROKEN"


def replay_segment_wall_history(
    events: Sequence[BookLevelEvent],
    *,
    segment: ReplaySegment,
    params: WallHistoryParams,
) -> dict[str, Any]:
    """Single-pass replay: extract walls at sample times without retaining book clones."""
    t0 = time.perf_counter()
    start = _ensure_aware(segment.segment_start_ts)
    end = _ensure_aware(segment.segment_end_ts)
    samples = wall_sample_times(
        start, end, interval_seconds=params.sample_interval_sec, warmup_seconds=params.warmup_seconds
    )
    summary = {
        "symbol": segment.symbol,
        "segment_id": segment.segment_id,
        "segment_start_ts": start.isoformat(),
        "segment_end_ts": end.isoformat(),
        "wall_sample_count": 0,
        "samples_after_warmup": len(samples),
        "bid_observation_count": 0,
        "ask_observation_count": 0,
        "bid_sequence_count": 0,
        "ask_sequence_count": 0,
        "tested_bid_sequences": 0,
        "tested_ask_sequences": 0,
        "broken_bid_sequences": 0,
        "broken_ask_sequences": 0,
        "disappeared_untested_bid_sequences": 0,
        "disappeared_untested_ask_sequences": 0,
        "max_bid_wall_notional": None,
        "max_ask_wall_notional": None,
        "max_bid_wall_multiple": None,
        "max_ask_wall_multiple": None,
        "average_bid_wall_distance_bps": None,
        "average_ask_wall_distance_bps": None,
        "runtime_sec": 0.0,
        "wall_history_status": "WALL_HISTORY_OK",
        "error_type": None,
        "error_message": None,
    }
    if not segment.is_replayable:
        summary["wall_history_status"] = "SKIPPED_NOT_REPLAYABLE"
        summary["runtime_sec"] = time.perf_counter() - t0
        return {
            "summary": summary,
            "observations": [],
            "candidates": [],
            "clusters": [],
            "sequences": [],
            "transitions": [],
            "errors": [],
        }
    if not samples:
        summary["wall_history_status"] = "WALL_HISTORY_OK_NO_POST_WARMUP"
        summary["runtime_sec"] = time.perf_counter() - t0
        return {
            "summary": summary,
            "observations": [],
            "candidates": [],
            "clusters": [],
            "sequences": [],
            "transitions": [],
            "errors": [],
        }

    remaining = list(samples)
    observations: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    oid = 1
    sample_mids: list[tuple[datetime, Decimal, Decimal | None, Decimal | None]] = []
    interval_hi: Decimal | None = None
    interval_lo: Decimal | None = None
    replayer = OrderBookReplayer()

    def _capture(ts: datetime) -> None:
        nonlocal oid, interval_hi, interval_lo
        if not replayer.book.has_snapshot:
            return
        mid = replayer.book.mid_price()
        if mid is None:
            return
        hi = interval_hi if interval_hi is not None else mid
        lo = interval_lo if interval_lo is not None else mid
        sample_mids.append((ts, mid, hi, lo))
        obs, cl, oid = observe_book_walls(
            replayer.book,
            symbol=segment.symbol,
            segment_id=segment.segment_id,
            sample_ts=ts,
            params=params,
            observation_id_start=oid,
        )
        observations.extend(obs)
        clusters.extend(cl)
        interval_hi = mid
        interval_lo = mid

    try:
        for message_type, update_id, seq, ts, levels in group_messages(events):
            ts = _ensure_aware(ts)
            if ts > end:
                break
            while remaining and remaining[0] < ts:
                _capture(remaining.pop(0))
            mid_before = replayer.book.mid_price() if replayer.book.has_snapshot else None
            replayer.apply_message(message_type, update_id, seq, ts, levels)
            mid_after = replayer.book.mid_price()
            for m in (mid_before, mid_after):
                if m is None:
                    continue
                interval_hi = m if interval_hi is None else max(interval_hi, m)
                interval_lo = m if interval_lo is None else min(interval_lo, m)
            while remaining and remaining[0] == ts:
                _capture(remaining.pop(0))
        while remaining:
            t = remaining.pop(0)
            if t <= end:
                _capture(t)
    except ReplayError as exc:
        summary["wall_history_status"] = "WALL_HISTORY_FAILED_REPLAY"
        summary["error_type"] = "ReplayError"
        summary["error_message"] = str(exc)
        summary["runtime_sec"] = time.perf_counter() - t0
        return {
            "summary": summary,
            "observations": observations,
            "candidates": [],
            "clusters": clusters,
            "sequences": [],
            "transitions": [],
            "errors": [
                {
                    "symbol": segment.symbol,
                    "segment_id": segment.segment_id,
                    "error_ts": None,
                    "source_update_id": replayer.book.last_update_id,
                    "error_type": "ReplayError",
                    "error_message": str(exc),
                    "details": "",
                }
            ],
        }
    except Exception as exc:  # noqa: BLE001
        summary["wall_history_status"] = "WALL_HISTORY_FAILED_PROCESSING"
        summary["error_type"] = type(exc).__name__
        summary["error_message"] = str(exc)
        summary["runtime_sec"] = time.perf_counter() - t0
        return {
            "summary": summary,
            "observations": observations,
            "candidates": [],
            "clusters": clusters,
            "sequences": [],
            "transitions": [],
            "errors": [
                {
                    "symbol": segment.symbol,
                    "segment_id": segment.segment_id,
                    "error_ts": None,
                    "source_update_id": None,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "details": "",
                }
            ],
        }

    preferred = resolution_name(params.preferred_bps)
    candidates = candidates_history_from_observations(observations, preferred_resolution=preferred)
    sequences, transitions = build_sequences_and_transitions(
        observations,
        symbol=segment.symbol,
        segment_id=segment.segment_id,
        segment_end_ts=end,
        params=params,
        preferred_only=True,
    )
    apply_test_break_flags(sequences, transitions, sample_mids=sample_mids, params=params)

    # strip internal fields from observations for CSV
    clean_obs = []
    for o in observations:
        row = {k: v for k, v in o.items() if not k.startswith("_")}
        clean_obs.append(row)

    bid_obs = [o for o in clean_obs if o.get("side") == "bid" and o.get("is_wall")]
    ask_obs = [o for o in clean_obs if o.get("side") == "ask" and o.get("is_wall")]
    bid_seq = [s for s in sequences if s.get("side") == "bid"]
    ask_seq = [s for s in sequences if s.get("side") == "ask"]

    def _avg(vals: list[float]) -> float | None:
        return sum(vals) / len(vals) if vals else None

    summary.update(
        {
            "wall_sample_count": len(samples),
            "bid_observation_count": len(bid_obs),
            "ask_observation_count": len(ask_obs),
            "bid_sequence_count": len(bid_seq),
            "ask_sequence_count": len(ask_seq),
            "tested_bid_sequences": sum(1 for s in bid_seq if s.get("was_tested")),
            "tested_ask_sequences": sum(1 for s in ask_seq if s.get("was_tested")),
            "broken_bid_sequences": sum(1 for s in bid_seq if s.get("was_broken")),
            "broken_ask_sequences": sum(1 for s in ask_seq if s.get("was_broken")),
            "disappeared_untested_bid_sequences": sum(
                1 for s in bid_seq if s.get("disappeared_before_test")
            ),
            "disappeared_untested_ask_sequences": sum(
                1 for s in ask_seq if s.get("disappeared_before_test")
            ),
            "max_bid_wall_notional": max((o.get("wall_notional") for o in bid_obs), default=None),
            "max_ask_wall_notional": max((o.get("wall_notional") for o in ask_obs), default=None),
            "max_bid_wall_multiple": max((o.get("wall_multiple") or 0 for o in bid_obs), default=None),
            "max_ask_wall_multiple": max((o.get("wall_multiple") or 0 for o in ask_obs), default=None),
            "average_bid_wall_distance_bps": _avg(
                [float(o["distance_to_mid_bps"]) for o in bid_obs if o.get("distance_to_mid_bps") is not None]
            ),
            "average_ask_wall_distance_bps": _avg(
                [float(o["distance_to_mid_bps"]) for o in ask_obs if o.get("distance_to_mid_bps") is not None]
            ),
            "runtime_sec": time.perf_counter() - t0,
            "wall_history_status": "WALL_HISTORY_OK",
        }
    )
    return {
        "summary": summary,
        "observations": clean_obs,
        "candidates": candidates,
        "clusters": clusters,
        "sequences": sequences,
        "transitions": transitions,
        "errors": [],
    }


def _obs_snapshot_features(
    observations: Sequence[Mapping[str, Any]],
    *,
    preferred_resolution: str,
) -> dict[str, Any]:
    walls = [
        o
        for o in observations
        if o.get("resolution") == preferred_resolution and o.get("is_wall")
    ]
    bids = [o for o in walls if o.get("side") == "bid"]
    asks = [o for o in walls if o.get("side") == "ask"]
    empty = {
        "wall_data_present": False,
        "nearest_bid_wall_price": None,
        "nearest_bid_wall_notional": None,
        "nearest_bid_wall_multiple": None,
        "nearest_bid_wall_percentile": None,
        "nearest_bid_wall_depth_share": None,
        "nearest_bid_wall_distance_bps": None,
        "nearest_ask_wall_price": None,
        "nearest_ask_wall_notional": None,
        "nearest_ask_wall_multiple": None,
        "nearest_ask_wall_percentile": None,
        "nearest_ask_wall_depth_share": None,
        "nearest_ask_wall_distance_bps": None,
        "strongest_bid_wall_price": None,
        "strongest_bid_wall_notional": None,
        "strongest_bid_wall_multiple": None,
        "strongest_bid_wall_distance_bps": None,
        "strongest_ask_wall_price": None,
        "strongest_ask_wall_notional": None,
        "strongest_ask_wall_multiple": None,
        "strongest_ask_wall_distance_bps": None,
        "bid_wall_count": 0,
        "ask_wall_count": 0,
        "bid_wall_total_notional": None,
        "ask_wall_total_notional": None,
        "wall_notional_imbalance": None,
    }
    if not walls:
        return empty

    def nearest(items: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        return min(items, key=lambda x: float(x.get("distance_to_mid_bps") or 1e18)) if items else None

    def strongest(items: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        return max(items, key=lambda x: Decimal(str(x.get("wall_notional") or 0))) if items else None

    nb, na = nearest(bids), nearest(asks)
    sb, sa = strongest(bids), strongest(asks)
    bid_tot = sum((Decimal(str(o.get("wall_notional") or 0)) for o in bids), Decimal("0"))
    ask_tot = sum((Decimal(str(o.get("wall_notional") or 0)) for o in asks), Decimal("0"))
    denom = bid_tot + ask_tot
    imb = _safe_float((bid_tot - ask_tot) / denom) if denom > 0 else None
    out = dict(empty)
    out.update(
        {
            "wall_data_present": True,
            "bid_wall_count": len(bids),
            "ask_wall_count": len(asks),
            "bid_wall_total_notional": _fmt(bid_tot),
            "ask_wall_total_notional": _fmt(ask_tot),
            "wall_notional_imbalance": imb,
        }
    )
    if nb:
        out.update(
            {
                "nearest_bid_wall_price": nb.get("wall_price"),
                "nearest_bid_wall_notional": nb.get("wall_notional"),
                "nearest_bid_wall_multiple": nb.get("wall_multiple"),
                "nearest_bid_wall_percentile": nb.get("percentile"),
                "nearest_bid_wall_depth_share": nb.get("depth_share"),
                "nearest_bid_wall_distance_bps": nb.get("distance_to_mid_bps"),
            }
        )
    if na:
        out.update(
            {
                "nearest_ask_wall_price": na.get("wall_price"),
                "nearest_ask_wall_notional": na.get("wall_notional"),
                "nearest_ask_wall_multiple": na.get("wall_multiple"),
                "nearest_ask_wall_percentile": na.get("percentile"),
                "nearest_ask_wall_depth_share": na.get("depth_share"),
                "nearest_ask_wall_distance_bps": na.get("distance_to_mid_bps"),
            }
        )
    if sb:
        out.update(
            {
                "strongest_bid_wall_price": sb.get("wall_price"),
                "strongest_bid_wall_notional": sb.get("wall_notional"),
                "strongest_bid_wall_multiple": sb.get("wall_multiple"),
                "strongest_bid_wall_distance_bps": sb.get("distance_to_mid_bps"),
            }
        )
    if sa:
        out.update(
            {
                "strongest_ask_wall_price": sa.get("wall_price"),
                "strongest_ask_wall_notional": sa.get("wall_notional"),
                "strongest_ask_wall_multiple": sa.get("wall_multiple"),
                "strongest_ask_wall_distance_bps": sa.get("distance_to_mid_bps"),
            }
        )
    return out


def join_timeline_with_walls(
    timeline_rows: Sequence[Mapping[str, Any]],
    *,
    observations: Sequence[Mapping[str, Any]],
    sequences: Sequence[Mapping[str, Any]],
    segments: Sequence[ReplaySegment],
    gaps: Sequence[ReplayGap],
    params: WallHistoryParams,
    transitions: Sequence[Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """LEFT-enrich ticker timeline with last wall sample at or before bucket_end.

    tested/broken flags are causal: only TESTED/BROKEN transitions with
    ``transition_ts <= bucket_end`` for the nearest wall's sequence_id.
    Final sequence fields are never projected backward into earlier buckets.
    """
    preferred = resolution_name(params.preferred_bps)
    by_ts: dict[datetime, list[Mapping[str, Any]]] = {}
    for o in observations:
        if o.get("resolution") != preferred:
            continue
        ts = _iso_to_dt(o["sample_ts"])
        by_ts.setdefault(ts, []).append(o)
    sample_times = sorted(by_ts.keys())

    # transitions by sequence_id
    tr_by_seq: dict[str, list[Mapping[str, Any]]] = {}
    for tr in transitions or []:
        sid = str(tr.get("wall_sequence_id") or "")
        if not sid:
            continue
        tr_by_seq.setdefault(sid, []).append(tr)

    gap_intervals = [
        (_ensure_aware(g.gap_start_ts), _ensure_aware(g.gap_end_ts)) for g in gaps
    ]

    def in_gap(ts: datetime) -> bool:
        for lo, hi in gap_intervals:
            if lo <= ts <= hi:
                return True
        return False

    def segment_for(ts: datetime) -> ReplaySegment | None:
        for seg in segments:
            if not seg.is_replayable:
                continue
            if _ensure_aware(seg.segment_start_ts) <= ts <= _ensure_aware(seg.segment_end_ts):
                return seg
        return None

    def _as_of_flags(seq_id: str | None, *, bucket_end: datetime) -> tuple[bool | None, bool | None]:
        if not seq_id:
            return None, None
        tested = broken = False
        for tr in tr_by_seq.get(seq_id, []):
            ttype = tr.get("transition_type")
            if ttype not in {"TESTED", "BROKEN", "TRADED_THROUGH"}:
                continue
            tts = tr.get("transition_ts")
            if tts is None:
                continue
            if _iso_to_dt(tts) > bucket_end:
                continue
            if ttype == "TESTED":
                tested = True
            elif ttype == "BROKEN":
                broken = True
        return tested, broken

    def _nearest_seq_id(obs_list: Sequence[Mapping[str, Any]], side: str) -> str | None:
        side_obs = [o for o in obs_list if o.get("side") == side and o.get("is_wall")]
        if not side_obs:
            return None
        # nearest by distance_to_mid_bps (same rule as snapshot features)
        best = min(
            side_obs,
            key=lambda o: float(o.get("distance_to_mid_bps") or 1e18),
        )
        sid = best.get("wall_sequence_id")
        if sid:
            return str(sid)
        # fallback: match active sequence by price at sample
        price = Decimal(str(best.get("wall_price") or best.get("_price_dec") or 0))
        sample_ts = _iso_to_dt(best["sample_ts"])
        for s in sequences:
            if str(s.get("side")) != side:
                continue
            if _iso_to_dt(s["first_seen_ts"]) > sample_ts:
                continue
            closed = s.get("closed_ts") or s.get("last_seen_ts")
            if closed and _iso_to_dt(closed) < sample_ts:
                continue
            try:
                if abs(Decimal(str(s.get("last_price"))) - price) / price <= Decimal("0.0005"):
                    return str(s.get("wall_sequence_id"))
            except Exception:  # noqa: BLE001
                continue
        return None

    out: list[dict[str, Any]] = []
    stale_limit = params.sample_interval_sec * params.stale_sample_intervals
    for row in timeline_rows:
        bucket_end = _iso_to_dt(row["bucket_end"])
        base = dict(row)
        wall_fields = {
            "wall_data_present": False,
            "wall_data_stale": False,
            "wall_segment_id": None,
            "wall_sample_ts": None,
            "wall_sample_age_sec": None,
            "nearest_bid_wall_age_sec": None,
            "nearest_ask_wall_age_sec": None,
            "nearest_bid_wall_tested": None,
            "nearest_bid_wall_broken": None,
            "nearest_ask_wall_tested": None,
            "nearest_ask_wall_broken": None,
        }
        wall_fields.update(_obs_snapshot_features([], preferred_resolution=preferred))

        if in_gap(bucket_end):
            base.update(wall_fields)
            out.append(base)
            continue

        seg = segment_for(bucket_end)
        chosen = None
        for ts in reversed(sample_times):
            if ts > bucket_end:
                continue
            if seg is not None and not (
                _ensure_aware(seg.segment_start_ts) <= ts <= _ensure_aware(seg.segment_end_ts)
            ):
                continue
            if in_gap(ts):
                continue
            chosen = ts
            break

        if chosen is None:
            base.update(wall_fields)
            out.append(base)
            continue

        age = (bucket_end - chosen).total_seconds()
        stale = age > stale_limit
        obs_at = by_ts.get(chosen) or []
        feats = _obs_snapshot_features(obs_at, preferred_resolution=preferred)
        wall_fields.update(feats)
        wall_fields.update(
            {
                "wall_data_present": bool(feats.get("wall_data_present")),
                "wall_data_stale": stale,
                "wall_segment_id": None if seg is None else seg.segment_id,
                "wall_sample_ts": chosen.isoformat(),
                "wall_sample_age_sec": age,
                "nearest_bid_wall_age_sec": age if feats.get("nearest_bid_wall_price") else None,
                "nearest_ask_wall_age_sec": age if feats.get("nearest_ask_wall_price") else None,
            }
        )
        bid_sid = _nearest_seq_id(obs_at, "bid") if feats.get("nearest_bid_wall_price") else None
        ask_sid = _nearest_seq_id(obs_at, "ask") if feats.get("nearest_ask_wall_price") else None
        bt, bb = _as_of_flags(bid_sid, bucket_end=bucket_end)
        at, ab = _as_of_flags(ask_sid, bucket_end=bucket_end)
        wall_fields["nearest_bid_wall_tested"] = bt
        wall_fields["nearest_bid_wall_broken"] = bb
        wall_fields["nearest_ask_wall_tested"] = at
        wall_fields["nearest_ask_wall_broken"] = ab
        base.update(wall_fields)
        out.append(base)
    return out


@dataclass
class WallHistoryResult:
    params: WallHistoryParams
    observations: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    clusters: list[dict[str, Any]] = field(default_factory=list)
    sequences: list[dict[str, Any]] = field(default_factory=list)
    transitions: list[dict[str, Any]] = field(default_factory=list)
    segment_summaries: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    timelines_with_walls: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    ok: bool = True
    error_message: str | None = None


def run_wall_history(
    db: Any,
    *,
    symbol: str,
    segments: Sequence[ReplaySegment],
    gaps: Sequence[ReplayGap],
    params: WallHistoryParams,
    timelines: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    replay_ok_segment_ids: set[str] | None = None,
) -> WallHistoryResult:
    """Process each replayable segment independently; free events after each segment."""
    result = WallHistoryResult(params=params)
    t_all = time.perf_counter()
    ok_ids = replay_ok_segment_ids
    for seg in segments:
        if not seg.is_replayable:
            result.segment_summaries.append(
                {
                    "symbol": symbol,
                    "segment_id": seg.segment_id,
                    "segment_start_ts": seg.segment_start_ts.isoformat(),
                    "segment_end_ts": seg.segment_end_ts.isoformat(),
                    "wall_sample_count": 0,
                    "samples_after_warmup": 0,
                    "bid_observation_count": 0,
                    "ask_observation_count": 0,
                    "bid_sequence_count": 0,
                    "ask_sequence_count": 0,
                    "tested_bid_sequences": 0,
                    "tested_ask_sequences": 0,
                    "broken_bid_sequences": 0,
                    "broken_ask_sequences": 0,
                    "disappeared_untested_bid_sequences": 0,
                    "disappeared_untested_ask_sequences": 0,
                    "max_bid_wall_notional": None,
                    "max_ask_wall_notional": None,
                    "max_bid_wall_multiple": None,
                    "max_ask_wall_multiple": None,
                    "average_bid_wall_distance_bps": None,
                    "average_ask_wall_distance_bps": None,
                    "runtime_sec": 0.0,
                    "wall_history_status": "SKIPPED_NOT_REPLAYABLE",
                    "error_type": None,
                    "error_message": seg.discard_reason,
                }
            )
            continue
        if ok_ids is not None and seg.segment_id not in ok_ids:
            result.segment_summaries.append(
                {
                    "symbol": symbol,
                    "segment_id": seg.segment_id,
                    "segment_start_ts": seg.segment_start_ts.isoformat(),
                    "segment_end_ts": seg.segment_end_ts.isoformat(),
                    "wall_sample_count": 0,
                    "samples_after_warmup": 0,
                    "bid_observation_count": 0,
                    "ask_observation_count": 0,
                    "bid_sequence_count": 0,
                    "ask_sequence_count": 0,
                    "tested_bid_sequences": 0,
                    "tested_ask_sequences": 0,
                    "broken_bid_sequences": 0,
                    "broken_ask_sequences": 0,
                    "disappeared_untested_bid_sequences": 0,
                    "disappeared_untested_ask_sequences": 0,
                    "max_bid_wall_notional": None,
                    "max_ask_wall_notional": None,
                    "max_bid_wall_multiple": None,
                    "max_ask_wall_multiple": None,
                    "average_bid_wall_distance_bps": None,
                    "average_ask_wall_distance_bps": None,
                    "runtime_sec": 0.0,
                    "wall_history_status": "WALL_HISTORY_FAILED_REPLAY",
                    "error_type": "replay_not_ok",
                    "error_message": "segment replay was not successful",
                }
            )
            result.errors.append(
                {
                    "symbol": symbol,
                    "segment_id": seg.segment_id,
                    "error_ts": None,
                    "source_update_id": None,
                    "error_type": "replay_not_ok",
                    "error_message": "segment replay was not successful",
                    "details": "",
                }
            )
            continue

        logger.info("Phase 4 wall history for %s", seg.segment_id)
        try:
            events = load_segment_events(db, symbol=symbol, segment=seg)
            bundle = replay_segment_wall_history(events, segment=seg, params=params)
            del events  # free segment events ASAP
        except Exception as exc:  # noqa: BLE001
            result.segment_summaries.append(
                {
                    "symbol": symbol,
                    "segment_id": seg.segment_id,
                    "segment_start_ts": seg.segment_start_ts.isoformat(),
                    "segment_end_ts": seg.segment_end_ts.isoformat(),
                    "wall_sample_count": 0,
                    "samples_after_warmup": 0,
                    "bid_observation_count": 0,
                    "ask_observation_count": 0,
                    "bid_sequence_count": 0,
                    "ask_sequence_count": 0,
                    "tested_bid_sequences": 0,
                    "tested_ask_sequences": 0,
                    "broken_bid_sequences": 0,
                    "broken_ask_sequences": 0,
                    "disappeared_untested_bid_sequences": 0,
                    "disappeared_untested_ask_sequences": 0,
                    "max_bid_wall_notional": None,
                    "max_ask_wall_notional": None,
                    "max_bid_wall_multiple": None,
                    "max_ask_wall_multiple": None,
                    "average_bid_wall_distance_bps": None,
                    "average_ask_wall_distance_bps": None,
                    "runtime_sec": 0.0,
                    "wall_history_status": "WALL_HISTORY_FAILED_PROCESSING",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            result.errors.append(
                {
                    "symbol": symbol,
                    "segment_id": seg.segment_id,
                    "error_ts": None,
                    "source_update_id": None,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "details": "",
                }
            )
            continue

        result.segment_summaries.append(bundle["summary"])
        result.observations.extend(bundle["observations"])
        result.candidates.extend(bundle["candidates"])
        result.clusters.extend(bundle["clusters"])
        result.sequences.extend(bundle["sequences"])
        result.transitions.extend(bundle["transitions"])
        result.errors.extend(bundle["errors"])

    if timelines:
        for tf, rows in timelines.items():
            result.timelines_with_walls[tf] = join_timeline_with_walls(
                rows,
                observations=result.observations,
                sequences=result.sequences,
                segments=segments,
                gaps=gaps,
                params=params,
                transitions=result.transitions,
            )
    else:
        result.warnings.append(
            "market context timelines absent; wall history ran without timeline join"
        )

    ok_segs = [
        s
        for s in result.segment_summaries
        if str(s.get("wall_history_status", "")).startswith("WALL_HISTORY_OK")
    ]
    failed_segs = [
        s
        for s in result.segment_summaries
        if str(s.get("wall_history_status", "")).startswith("WALL_HISTORY_FAILED")
    ]
    result.stats = {
        "wall_history_requested": True,
        "wall_history_ok": len(failed_segs) == 0 and len(ok_segs) > 0,
        "wall_sample_interval_sec": params.sample_interval_sec,
        "wall_warmup_seconds": params.warmup_seconds,
        "wall_segments_total": len([s for s in segments if s.is_replayable]),
        "wall_segments_ok": len(ok_segs),
        "wall_segments_failed": len(failed_segs),
        "wall_samples_total": sum(int(s.get("wall_sample_count") or 0) for s in result.segment_summaries),
        "wall_observations_total": len(result.observations),
        "wall_clusters_total": len(result.clusters),
        "wall_sequences_total": len(result.sequences),
        "wall_transitions_total": len(result.transitions),
        "bid_wall_sequences": sum(1 for s in result.sequences if s.get("side") == "bid"),
        "ask_wall_sequences": sum(1 for s in result.sequences if s.get("side") == "ask"),
        "tested_wall_sequences": sum(1 for s in result.sequences if s.get("was_tested")),
        "broken_wall_sequences": sum(1 for s in result.sequences if s.get("was_broken")),
        "disappeared_before_test_sequences": sum(
            1 for s in result.sequences if s.get("disappeared_before_test")
        ),
        "timeline_with_walls_rows_1m": len(result.timelines_with_walls.get("1m") or []),
        "timeline_with_walls_rows_5m": len(result.timelines_with_walls.get("5m") or []),
        "wall_history_runtime_sec_total": time.perf_counter() - t_all,
        "price_path_source": "segment_replay_mid_high_low",
        "preferred_resolution": resolution_name(params.preferred_bps),
        "merge_split_supported": False,
    }
    result.ok = bool(result.stats["wall_history_ok"])
    if len(ok_segs) == 0 and len([s for s in segments if s.is_replayable]) > 0:
        result.ok = False
        result.error_message = "no segments produced wall history successfully"
        result.stats["wall_history_ok"] = False
    return result


def decide_phase4_wall(*, ok: bool, gap_count: int, has_failures: bool, has_success: bool) -> str:
    if not has_success and not ok:
        return "FULL_HISTORY_WALL_HISTORY_FAILED"
    if has_failures:
        return "FULL_HISTORY_WALL_HISTORY_PARTIAL"
    if gap_count > 0:
        return "FULL_HISTORY_WALL_HISTORY_COMPLETE_WITH_GAPS"
    return "FULL_HISTORY_WALL_HISTORY_COMPLETE"


def decide_full_analysis(
    *,
    integrity_ok: bool,
    gap_count: int,
    module_decisions: Sequence[str],
) -> str:
    """Priority: integrity fail → FAILED; any module fail/partial → PARTIAL; gaps → WITH_GAPS; else COMPLETE."""
    if not integrity_ok:
        return "FULL_HISTORY_ANALYSIS_FAILED"
    if any(d.endswith("_FAILED") for d in module_decisions):
        if all(d.endswith("_FAILED") for d in module_decisions):
            return "FULL_HISTORY_ANALYSIS_FAILED"
        return "FULL_HISTORY_ANALYSIS_PARTIAL"
    if any("PARTIAL" in d for d in module_decisions):
        return "FULL_HISTORY_ANALYSIS_PARTIAL"
    if gap_count > 0 or any("WITH_GAPS" in d for d in module_decisions):
        return "FULL_HISTORY_ANALYSIS_COMPLETE_WITH_GAPS"
    return "FULL_HISTORY_ANALYSIS_COMPLETE"


def check_wall_history_integrity(
    *,
    observations: Sequence[Mapping[str, Any]],
    sequences: Sequence[Mapping[str, Any]],
    transitions: Sequence[Mapping[str, Any]],
    segments: Sequence[ReplaySegment],
    warmup_seconds: int,
    timelines_with_walls: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    errs: list[str] = []
    warns: list[str] = []
    seg_by = {s.segment_id: s for s in segments}
    obs_ids = [o.get("wall_observation_id") for o in observations]
    if len(obs_ids) != len(set(obs_ids)):
        errs.append("duplicate wall_observation_id")
    seq_ids = [s.get("wall_sequence_id") for s in sequences]
    if len(seq_ids) != len(set(seq_ids)):
        errs.append("duplicate wall_sequence_id")
    for o in observations:
        seg = seg_by.get(str(o.get("segment_id")))
        if seg is None or not seg.is_replayable:
            errs.append(f"observation for non-replayable/unknown segment {o.get('segment_id')}")
            continue
        ts = _iso_to_dt(o["sample_ts"])
        if ts < _ensure_aware(seg.segment_start_ts) or ts > _ensure_aware(seg.segment_end_ts):
            errs.append(f"observation outside segment bounds {o.get('wall_observation_id')}")
        feature_start = _ensure_aware(seg.segment_start_ts) + timedelta(seconds=warmup_seconds)
        if ts < feature_start:
            errs.append(f"observation during warmup {o.get('wall_observation_id')}")
        for key in ("wall_multiple", "distance_to_mid_bps", "spread_bps"):
            v = o.get(key)
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                errs.append(f"non-finite {key} in observation")
    for s in sequences:
        if _iso_to_dt(s["first_seen_ts"]) > _iso_to_dt(s["last_seen_ts"]):
            errs.append(f"sequence first>last {s.get('wall_sequence_id')}")
        if float(s.get("age_seconds") or 0) < 0:
            errs.append(f"negative age {s.get('wall_sequence_id')}")
        if int(s.get("sample_count") or 0) < 1:
            errs.append(f"sample_count < 1 {s.get('wall_sequence_id')}")
        # no cross-segment: sequence id embeds segment
        if str(s.get("segment_id")) not in str(s.get("wall_sequence_id")):
            errs.append(f"sequence id missing segment {s.get('wall_sequence_id')}")
        if s.get("disappeared_before_test") and s.get("was_tested"):
            errs.append(f"disappeared_before_test with was_tested {s.get('wall_sequence_id')}")
    known = set(seq_ids)
    for tr in transitions:
        if tr.get("wall_sequence_id") not in known:
            errs.append(f"transition without sequence {tr.get('wall_sequence_id')}")
    if timelines_with_walls:
        for tf, rows in timelines_with_walls.items():
            for r in rows:
                if not r.get("wall_sample_ts"):
                    continue
                sample = _iso_to_dt(r["wall_sample_ts"])
                bend = _iso_to_dt(r["bucket_end"])
                if sample > bend:
                    errs.append(f"future wall sample in timeline {tf}")
    return {"ok": len(errs) == 0, "errors": errs, "warnings": warns}


WALL_OBSERVATION_HEADERS = [
    "symbol", "segment_id", "sample_ts", "resolution", "bucket_size", "side", "wall_price",
    "wall_quantity", "wall_notional", "wall_multiple", "percentile", "depth_share",
    "distance_to_mid_abs", "distance_to_mid_pct", "distance_to_mid_bps",
    "local_median_notional", "local_mean_notional", "rank_on_side", "is_wall",
    "cluster_id", "cluster_start_price", "cluster_end_price", "cluster_total_notional", "cluster_bucket_count",
    "best_bid", "best_ask", "mid_price", "spread", "spread_bps",
    "active_bid_levels", "active_ask_levels", "total_bid_depth_notional", "total_ask_depth_notional",
    "side_depth_notional", "wall_share_of_side_depth", "feature_emission_allowed",
    "source_update_id", "source_cross_sequence", "wall_observation_id",
]

WALL_CANDIDATE_HEADERS = [
    "symbol", "segment_id", "sample_ts", "wall_observation_id", "side", "wall_price", "wall_notional",
    "wall_multiple", "percentile", "depth_share", "distance_bps", "cluster_id",
    "is_near_wall", "is_dominant_wall", "is_strongest_side_wall", "best_bid", "best_ask", "mid_price",
]

WALL_CLUSTER_HEADERS = [
    "symbol", "segment_id", "sample_ts", "resolution", "cluster_id", "side", "start_price", "end_price",
    "strongest_bucket_price", "strongest_bucket_notional", "total_quantity", "total_notional", "bucket_count",
    "distance_min_bps", "distance_max_bps", "wall_multiple_max", "percentile_max", "depth_share_total",
]

WALL_SEQUENCE_HEADERS = [
    "symbol", "segment_id", "wall_sequence_id", "side", "resolution", "first_seen_ts", "last_seen_ts",
    "closed_ts", "age_seconds", "sample_count", "first_price", "last_price", "min_price", "max_price",
    "price_move_abs", "price_move_bps", "first_notional", "last_notional", "min_notional", "max_notional",
    "notional_change_abs", "notional_change_pct", "max_wall_multiple", "max_percentile", "max_depth_share",
    "min_distance_bps", "max_distance_bps", "was_near_price", "was_tested", "was_broken",
    "touched", "traded_through", "confirmed_broken",
    "disappeared_before_test", "ended_by_segment", "end_reason",
    "min_test_distance_bps", "first_test_ts", "first_traded_through_ts", "confirmed_break_ts",
    "test_price_source",
]

WALL_TRANSITION_HEADERS = [
    "symbol", "segment_id", "wall_sequence_id", "transition_ts", "side", "resolution", "transition_type",
    "previous_price", "current_price", "price_change_bps", "previous_notional", "current_notional",
    "notional_change_abs", "notional_change_pct", "previous_distance_bps", "current_distance_bps",
    "sample_gap_seconds", "details",
]

WALL_SEGMENT_SUMMARY_HEADERS = [
    "symbol", "segment_id", "segment_start_ts", "segment_end_ts", "wall_sample_count", "samples_after_warmup",
    "bid_observation_count", "ask_observation_count", "bid_sequence_count", "ask_sequence_count",
    "tested_bid_sequences", "tested_ask_sequences", "broken_bid_sequences", "broken_ask_sequences",
    "disappeared_untested_bid_sequences", "disappeared_untested_ask_sequences",
    "max_bid_wall_notional", "max_ask_wall_notional", "max_bid_wall_multiple", "max_ask_wall_multiple",
    "average_bid_wall_distance_bps", "average_ask_wall_distance_bps", "runtime_sec",
    "wall_history_status", "error_type", "error_message",
]

WALL_ERROR_HEADERS = [
    "symbol", "segment_id", "error_ts", "source_update_id", "error_type", "error_message", "details",
]

TIMELINE_WALL_EXTRA_HEADERS = [
    "wall_data_present", "wall_data_stale", "wall_segment_id", "wall_sample_ts", "wall_sample_age_sec",
    "nearest_bid_wall_price", "nearest_bid_wall_notional", "nearest_bid_wall_multiple",
    "nearest_bid_wall_percentile", "nearest_bid_wall_depth_share", "nearest_bid_wall_distance_bps",
    "nearest_bid_wall_age_sec", "nearest_bid_wall_tested", "nearest_bid_wall_broken",
    "nearest_ask_wall_price", "nearest_ask_wall_notional", "nearest_ask_wall_multiple",
    "nearest_ask_wall_percentile", "nearest_ask_wall_depth_share", "nearest_ask_wall_distance_bps",
    "nearest_ask_wall_age_sec", "nearest_ask_wall_tested", "nearest_ask_wall_broken",
    "strongest_bid_wall_price", "strongest_bid_wall_notional", "strongest_bid_wall_multiple", "strongest_bid_wall_distance_bps",
    "strongest_ask_wall_price", "strongest_ask_wall_notional", "strongest_ask_wall_multiple", "strongest_ask_wall_distance_bps",
    "bid_wall_count", "ask_wall_count", "bid_wall_total_notional", "ask_wall_total_notional", "wall_notional_imbalance",
]


def phase4_output_files(timeframes: Sequence[str] | None = None) -> list[str]:
    files = list(PHASE4_OUTPUT_FILES)
    for tf in timeframes or ("1m", "5m"):
        files.append(f"analysis_timeline_{tf}_with_walls.csv")
    return files
