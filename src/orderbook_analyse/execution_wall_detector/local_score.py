"""Near-market bucketing and local dominance scoring for EXECUTION walls."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Iterable, Sequence

from orderbook_analyse.dynamic_wall_detector import choose_bucket_size, infer_tick_size
from orderbook_analyse.execution_wall_detector.types import ExecutionWallParams, LocalLevelMetrics


def distance_bps(price: float, mid: float) -> float:
    if mid <= 0 or price <= 0:
        return float("inf")
    return abs(price - mid) / mid * 10_000.0


def band_label(dist_bps: float, bands: Sequence[float]) -> str:
    edges = sorted(float(x) for x in bands)
    if not edges:
        return "unknown"
    prev = edges[0]
    for e in edges[1:]:
        if dist_bps <= e + 1e-12:
            return f"{prev:g}-{e:g}"
        prev = e
    return f">{edges[-1]:g}"


def resolve_bucket_price(
    price: float,
    *,
    side: str,
    mid: float,
    tick: float,
    params: ExecutionWallParams,
) -> float:
    mode = str(params.bucket_mode).lower()
    px = Decimal(str(price))
    tick_d = Decimal(str(tick))
    if mode == "exact":
        return float(px)
    if mode == "bps":
        size = choose_bucket_size(mid, tick_d, float(params.bucket_bps))
    else:  # ticks
        n = max(1, int(params.bucket_ticks))
        size = tick_d * Decimal(n)
    if str(side).lower() in {"ask", "sell"}:
        mult = (px / size).to_integral_value(rounding=ROUND_CEILING)
    else:
        mult = (px / size).to_integral_value(rounding=ROUND_FLOOR)
    return float(mult * size)


def _percentile(sorted_vals: Sequence[float], value: float) -> float:
    if not sorted_vals:
        return 0.0
    n = sum(1 for v in sorted_vals if v <= value)
    return 100.0 * n / len(sorted_vals)


def score_near_levels(
    *,
    side: str,
    levels: dict[float, float],
    mid: float,
    best_bid: float | None,
    best_ask: float | None,
    tick: float,
    params: ExecutionWallParams,
    opposite_levels: dict[float, float] | None = None,
) -> list[LocalLevelMetrics]:
    """Score near-market buckets using local (not global-side) dominance."""
    side_l = "ask" if str(side).lower() in {"ask", "sell"} else "bid"
    max_d = float(params.max_distance_bps)

    # Aggregate exact levels into micro-buckets inside the near band.
    bucket_qty: dict[float, float] = defaultdict(float)
    bucket_notional: dict[float, float] = defaultdict(float)
    bucket_price_min: dict[float, float] = {}
    bucket_price_max: dict[float, float] = {}
    for px, qty in levels.items():
        if qty <= 0:
            continue
        px_f = float(px)
        qty_f = float(qty)
        if side_l == "ask" and px_f < mid:
            continue
        if side_l == "bid" and px_f > mid:
            continue
        d = distance_bps(px_f, mid)
        if d > max_d:
            continue
        bpx = resolve_bucket_price(
            px_f, side=side_l, mid=mid, tick=tick, params=params
        )
        bucket_qty[bpx] += qty_f
        bucket_notional[bpx] += px_f * qty_f
        bucket_price_min[bpx] = min(bucket_price_min.get(bpx, px_f), px_f)
        bucket_price_max[bpx] = max(bucket_price_max.get(bpx, px_f), px_f)

    if not bucket_qty:
        return []

    near_buckets = []
    for bpx, qty in bucket_qty.items():
        dist = distance_bps(bpx, mid)
        near_buckets.append((bpx, qty, dist, bucket_notional[bpx]))

    same_qtys = [q for _, q, _, _ in near_buckets]
    same_depth = sum(n for *_, n in near_buckets)
    sorted_qtys = sorted(same_qtys)

    opp_depth = 0.0
    if opposite_levels:
        for px, qty in opposite_levels.items():
            if qty <= 0:
                continue
            px_f = float(px)
            if distance_bps(px_f, mid) <= max_d:
                opp_depth += px_f * float(qty)
    elif side_l == "ask" and best_bid is not None:
        opp_depth = float(best_bid)  # weak proxy only
    elif side_l == "bid" and best_ask is not None:
        opp_depth = float(best_ask)

    radius = max(1, int(params.local_radius_ticks)) * float(tick)
    ranked = sorted(near_buckets, key=lambda t: t[3], reverse=True)
    rank_map = {bpx: i + 1 for i, (bpx, *_) in enumerate(ranked)}

    # Rank within the near-touch sub-band (0–near_touch_bps) separately.
    near_touch = [t for t in near_buckets if t[2] <= params.near_touch_bps + 1e-12]
    near_touch_ranked = sorted(near_touch, key=lambda t: t[3], reverse=True)
    near_touch_rank = {bpx: i + 1 for i, (bpx, *_) in enumerate(near_touch_ranked)}
    near_touch_qtys = sorted(q for _, q, _, _ in near_touch)

    out: list[LocalLevelMetrics] = []
    for bpx, qty, dist, notional in near_buckets:
        local = [
            (p, q)
            for p, q, _, _ in near_buckets
            if abs(p - bpx) <= radius + 1e-15
        ]
        local_qtys = [q for _, q in local] or [qty]
        local_sorted = sorted(local_qtys)
        med = float(local_sorted[len(local_sorted) // 2])
        mean = sum(local_qtys) / len(local_qtys)
        multiple = (qty / med) if med > 0 else 0.0
        # Local percentile within neighborhood (not the whole 0–max band).
        pct = _percentile(local_sorted, qty)
        share = (notional / same_depth) if same_depth > 0 else 0.0
        imb = 0.0
        if same_depth + opp_depth > 0:
            imb = (same_depth - opp_depth) / (same_depth + opp_depth)

        base_noise_ok = qty >= params.min_level_qty and notional >= params.min_level_notional
        dominant = (
            multiple >= params.local_multiple_min
            or pct >= params.local_percentile_min
            or share >= params.local_depth_share_min
        )
        near_soft = False
        if dist <= params.near_touch_bps + 1e-12 and near_touch_qtys:
            nt_pct = _percentile(near_touch_qtys, qty)
            nt_rank = near_touch_rank.get(bpx, 999)
            near_soft = (
                multiple >= params.near_touch_multiple_min
                or nt_pct >= params.near_touch_percentile_min
                or nt_rank <= params.near_touch_rank_max
            )
        is_cand = base_noise_ok and (dominant or near_soft)
        # Representative exact price: volume-weighted mid of bucket range
        rep_price = (bucket_price_min[bpx] + bucket_price_max[bpx]) / 2.0
        out.append(
            LocalLevelMetrics(
                side=side_l,
                price=rep_price,
                bucket_price=bpx,
                level_qty=qty,
                level_notional=notional,
                distance_bps=dist,
                same_side_near_depth=same_depth,
                same_side_local_median_qty=med,
                same_side_local_mean_qty=mean,
                same_side_local_percentile=pct,
                opposite_side_near_depth=float(opp_depth),
                local_depth_share=share,
                local_multiple=multiple,
                book_imbalance_near=imb,
                level_rank_within_near_band=rank_map.get(bpx, 0),
                is_candidate=is_cand,
                band_label=band_label(dist, params.distance_bands_bps),
            )
        )
    return out


def infer_tick_from_levels(levels: Iterable[float], fallback: float = 0.0001) -> float:
    prices = [Decimal(str(p)) for p in levels]
    try:
        return float(infer_tick_size(prices, fallback=Decimal(str(fallback))))
    except Exception:
        return fallback


def book_side_to_float_map(side_book: dict) -> dict[float, float]:
    """Convert Decimal book side to float price->qty."""
    out: dict[float, float] = {}
    for px, qty in side_book.items():
        q = float(qty)
        if q > 0:
            out[float(px)] = q
    return out
