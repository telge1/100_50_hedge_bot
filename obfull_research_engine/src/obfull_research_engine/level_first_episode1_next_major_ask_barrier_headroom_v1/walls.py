"""Past-only ask-wall candidates from causal Full-OB snapshot."""

from __future__ import annotations

from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from ..timeparse import format_utc_z
from . import (
    EPSILON,
    MAJOR_PERCENTILE_VIEWS,
    MIN_WALL_CANDIDATE_QTY,
    SCAN_MAX_DISTANCE_PCT,
    TICK_SIZE,
)


def _percentile_rank(value: float, past: list[float]) -> float | None:
    if len(past) < 1:
        return None
    n = sum(1 for x in past if x <= value + 1e-15)
    return n / len(past)


def build_past_baselines(
    scored_walls: list[dict[str, Any]],
    *,
    decision_time: str,
) -> dict[str, list[float]]:
    """Past-only baselines: walls with first_visible_at / start_time < decision_time."""
    dt = _as_dt(decision_time)
    sizes: list[float] = []
    notionals: list[float] = []
    for w in scored_walls:
        t0 = w.get("first_visible_at") or w.get("start_time")
        if not t0:
            continue
        if _as_dt(t0) >= dt:
            continue
        q = float(w.get("initial_qty") or w.get("peak_qty") or 0.0)
        px = float(w.get("price") or w.get("wall_price") or 0.0)
        if q <= EPSILON or px <= EPSILON:
            continue
        sizes.append(q)
        notionals.append(q * px)
    return {"sizes": sizes, "notionals": notionals}


def _active_wall_meta(
    scored_walls: list[dict[str, Any]],
    *,
    price: float,
    decision_time: str,
) -> dict[str, Any] | None:
    dt = _as_dt(decision_time)
    hits = []
    for w in scored_walls:
        px = float(w.get("price") or w.get("wall_price") or 0.0)
        if abs(px - price) > 1e-9:
            continue
        t0 = w.get("first_visible_at") or w.get("start_time")
        if not t0 or _as_dt(t0) > dt:
            continue
        end = w.get("end_time")
        if end and _as_dt(end) < dt:
            continue
        hits.append(w)
    if not hits:
        return None
    # Prefer largest initial qty among matching gens visible at decision
    hits.sort(key=lambda w: -float(w.get("initial_qty") or 0.0))
    return hits[0]


def wall_candidates_from_book(
    *,
    asks: dict[float, float],
    entry_price: float,
    decision_time: str,
    replay_epoch: int | None,
    scored_walls: list[dict[str, Any]],
    max_input_available_at: str | None,
    min_qty: float = MIN_WALL_CANDIDATE_QTY,
    scan_max_distance_pct: float = SCAN_MAX_DISTANCE_PCT,
) -> list[dict[str, Any]]:
    """Ask levels above entry with past-only ranks/percentiles. No future size."""
    entry = float(entry_price)
    lim = entry * (1.0 + float(scan_max_distance_pct) / 100.0)
    baselines = build_past_baselines(scored_walls, decision_time=decision_time)
    past_sizes = baselines["sizes"]
    past_notionals = baselines["notionals"]

    raw: list[dict[str, Any]] = []
    for px, qty in asks.items():
        p = float(px)
        q = float(qty)
        if p <= entry + 1e-12:
            continue
        if p > lim + 1e-12:
            continue
        if q + 1e-15 < min_qty:
            continue
        notional = p * q
        dist_ticks = (p - entry) / TICK_SIZE
        dist_pct = (p / entry - 1.0) * 100.0
        dist_bps = dist_pct * 100.0
        size_pct = _percentile_rank(q, past_sizes)
        notional_pct = _percentile_rank(notional, past_notionals)
        meta = _active_wall_meta(scored_walls, price=p, decision_time=decision_time)
        first_vis = (meta or {}).get("first_visible_at") or (meta or {}).get("start_time")
        age_ms = None
        if first_vis:
            age_ms = int(round((_as_dt(decision_time) - _as_dt(first_vis)).total_seconds() * 1000.0))
        # local depth share among scanned asks
        raw.append(
            {
                "price": p,
                "qty_base": q,
                "notional_usdt": notional,
                "distance_ticks": dist_ticks,
                "distance_bps": dist_bps,
                "distance_pct": dist_pct,
                "rolling_size_percentile": size_pct,
                "rolling_notional_percentile": notional_pct,
                "robust_size_zscore": None,  # filled below if possible
                "first_visible_at": first_vis,
                "age_at_decision_ms": age_ms,
                "replay_epoch": replay_epoch,
                "wall_generation_id": (meta or {}).get("wall_generation_id"),
                "pre_existing_at_decision": bool(first_vis) and _as_dt(first_vis) < _as_dt(decision_time),
                "max_input_available_at": max_input_available_at,
                "coverage_ok": True,
                "past_baseline_n": len(past_sizes),
                "effective_size_percentile": size_pct,
                "percentile_basis": "past_only_wall_generations" if size_pct is not None else None,
            }
        )

    total_qty = sum(r["qty_base"] for r in raw) or None
    # robust z vs past sizes
    if past_sizes:
        med = sorted(past_sizes)[len(past_sizes) // 2]
        mad = sorted(abs(x - med) for x in past_sizes)[len(past_sizes) // 2]
        for r in raw:
            if mad and mad > EPSILON:
                r["robust_size_zscore"] = (r["qty_base"] - med) / mad
            if total_qty:
                r["local_depth_share"] = r["qty_base"] / total_qty
            else:
                r["local_depth_share"] = None
    else:
        for r in raw:
            r["local_depth_share"] = (r["qty_base"] / total_qty) if total_qty else None

    by_size = sorted(raw, key=lambda r: (-r["qty_base"], r["price"]))
    by_notional = sorted(raw, key=lambda r: (-r["notional_usdt"], r["price"]))
    size_rank = {id(r): i + 1 for i, r in enumerate(by_size)}
    notional_rank = {id(r): i + 1 for i, r in enumerate(by_notional)}
    n = len(raw)
    for r in raw:
        r["rank_by_size"] = size_rank[id(r)]
        r["rank_by_notional"] = notional_rank[id(r)]
        # Contemporaneous causal percentile among visible asks above entry
        # (used only when past-only baseline is insufficient).
        r["contemporaneous_size_percentile"] = 1.0 - (size_rank[id(r)] - 1) / n if n else None
        if r.get("rolling_size_percentile") is None:
            r["percentile_basis"] = "contemporaneous_visible_asks"
            r["effective_size_percentile"] = r["contemporaneous_size_percentile"]
        else:
            r["percentile_basis"] = "past_only_wall_generations"
            r["effective_size_percentile"] = r["rolling_size_percentile"]

    return sorted(raw, key=lambda r: r["price"])


def next_wall_at_percentile(
    candidates: list[dict[str, Any]],
    *,
    percentile: float,
) -> dict[str, Any] | None:
    """Nearest (lowest price) wall with effective size percentile >= threshold."""
    hits = [
        c
        for c in candidates
        if c.get("effective_size_percentile") is not None
        and float(c["effective_size_percentile"]) + 1e-15 >= float(percentile)
    ]
    if not hits:
        # fall back to rolling_size_percentile field if effective missing
        hits = [
            c
            for c in candidates
            if c.get("rolling_size_percentile") is not None
            and float(c["rolling_size_percentile"]) + 1e-15 >= float(percentile)
        ]
    if not hits:
        return None
    return min(hits, key=lambda c: c["price"])


def largest_within_pct(
    candidates: list[dict[str, Any]],
    *,
    entry: float,
    within_pct: float,
) -> dict[str, Any] | None:
    lim = float(entry) * (1.0 + float(within_pct) / 100.0)
    hits = [c for c in candidates if c["price"] <= lim + 1e-12]
    if not hits:
        return None
    return max(hits, key=lambda c: (c["qty_base"], c["notional_usdt"]))


def major_views(candidates: list[dict[str, Any]], *, entry: float) -> dict[str, Any]:
    out: dict[str, Any] = {"by_percentile": {}, "largest_within_pct": {}}
    for q in MAJOR_PERCENTILE_VIEWS:
        key = f"Q{int(round(q * 100))}"
        w = next_wall_at_percentile(candidates, percentile=q)
        out["by_percentile"][key] = w
    from . import LARGEST_WITHIN_PCT_VIEWS

    for pct in LARGEST_WITHIN_PCT_VIEWS:
        key = f"within_{pct:.2f}pct"
        out["largest_within_pct"][key] = largest_within_pct(candidates, entry=entry, within_pct=pct)
    return out
