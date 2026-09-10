"""Past-only wall relevance scoring (outcome-blind)."""

from __future__ import annotations

import math
from typing import Any

from ..drilldown.aggregation_100ms import _as_dt
from . import (
    EPSILON,
    MAJOR_WALL_PERCENTILE,
    MIN_BASELINE_SAMPLES,
    ORIGINAL_WALL_PRICE,
    RELEVANCE_CONTRACT,
    RELEVANCE_LOCAL_DEPTH_SHARE_FLOOR,
    RELEVANCE_MEDIAN_RATIO_FLOOR,
    RELEVANCE_PERCENTILE_FLOOR,
    TICK_SIZE,
    WALL_RELEVANCE_STATUS,
    ZONE_HIGH,
    ZONE_LOW,
)


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def _mad(xs: list[float], med: float | None) -> float | None:
    if med is None or not xs:
        return None
    return _median([abs(x - med) for x in xs])


def _percentile_rank(value: float, past: list[float]) -> float | None:
    """Fraction of past values <= value (past-only). None if insufficient history."""
    if len(past) < 1:
        return None
    n = sum(1 for x in past if x <= value + 1e-15)
    return n / len(past)


def score_generations_past_only(
    generations: list[dict[str, Any]],
    *,
    liquidity_by_time: list[dict[str, Any]] | None = None,
    wall_price: float = ORIGINAL_WALL_PRICE,
) -> list[dict[str, Any]]:
    """Annotate each generation with past-only relevance features at first_visible_at.

    Order: process by start_time ascending; baseline updated AFTER scoring each wall
    so the wall itself does not enter its own baseline.
    """
    gens = sorted(generations, key=lambda g: (_as_dt(g["start_time"]), g["price"], g["generation_index"]))
    past_sizes: list[float] = []
    past_notionals: list[float] = []
    liq_idx = []
    if liquidity_by_time:
        liq_idx = sorted(liquidity_by_time, key=lambda r: _as_dt(r["timestamp"]))

    def liq_asof(ts):
        last = None
        for r in liq_idx:
            if _as_dt(r["timestamp"]) <= ts:
                last = r
            else:
                break
        return last

    out: list[dict[str, Any]] = []
    for g in gens:
        t0 = _as_dt(g["start_time"])
        init_q = float(g.get("initial_qty") or 0.0)
        mid = g.get("mid_at_start")
        if mid is None and liq_asof(t0):
            mid = liq_asof(t0).get("midprice")
        init_notional = init_q * float(g["price"])
        liq = liq_asof(t0)
        total_ask = float((liq or {}).get("total_ask_qty") or 0.0)
        local_depth_share = (init_q / total_ask) if total_ask > EPSILON else None

        med = _median(past_sizes)
        mad = _mad(past_sizes, med)
        size_pct = _percentile_rank(init_q, past_sizes)
        notional_pct = _percentile_rank(init_notional, past_notionals)
        vs_median = (init_q / med) if med and med > EPSILON else None
        robust_z = None
        if mad is not None and mad > EPSILON and med is not None:
            robust_z = (init_q - med) / mad

        dist_mid = None
        if mid is not None:
            dist_mid = (float(g["price"]) - float(mid)) / TICK_SIZE
        # distance to zone: ask wall above zone → ticks above zone_high
        dist_zone = (float(g["price"]) - float(ZONE_HIGH)) / TICK_SIZE

        life_to_now = None
        if g.get("end_time"):
            life_to_now = (_as_dt(g["end_time"]) - t0).total_seconds() * 1000.0
        # at entry, lifetime so far is 0; keep end-known duration as descriptive only under separate key

        is_original = abs(float(g["price"]) - wall_price) <= 1e-9
        relevant = False
        reasons = []
        if is_original:
            relevant = True
            reasons.append("original_wall_price")
        if size_pct is not None and size_pct >= RELEVANCE_PERCENTILE_FLOOR and len(past_sizes) >= MIN_BASELINE_SAMPLES:
            relevant = True
            reasons.append(f"rolling_size_pct>={RELEVANCE_PERCENTILE_FLOOR}")
        if vs_median is not None and vs_median >= RELEVANCE_MEDIAN_RATIO_FLOOR and len(past_sizes) >= MIN_BASELINE_SAMPLES:
            relevant = True
            reasons.append(f"vs_median>={RELEVANCE_MEDIAN_RATIO_FLOOR}")
        if local_depth_share is not None and local_depth_share >= RELEVANCE_LOCAL_DEPTH_SHARE_FLOOR:
            relevant = True
            reasons.append(f"local_depth_share>={RELEVANCE_LOCAL_DEPTH_SHARE_FLOOR}")
        # Cold start: insufficient baseline — only original wall or large absolute depth share
        if len(past_sizes) < MIN_BASELINE_SAMPLES and not relevant:
            if local_depth_share is not None and local_depth_share >= RELEVANCE_LOCAL_DEPTH_SHARE_FLOOR:
                relevant = True
                reasons.append("cold_start_depth_share")

        major = bool(
            size_pct is not None
            and size_pct >= MAJOR_WALL_PERCENTILE
            and len(past_sizes) >= MIN_BASELINE_SAMPLES
        )

        row = {
            **g,
            "first_visible_at": g["start_time"],
            "first_relevant_at": g["start_time"] if relevant else None,
            "initial_notional": init_notional,
            "local_depth_share": local_depth_share,
            "rolling_size_percentile": size_pct,
            "rolling_notional_percentile": notional_pct,
            "size_vs_local_median": vs_median,
            "size_vs_mad_robust_z": robust_z,
            "distance_from_mid_ticks_at_entry": dist_mid,
            "distance_from_zone_ticks": dist_zone,
            "lifetime_ms_if_ended": life_to_now,
            "pre_existing": str(g.get("layer_class") or "").startswith("PRE_EXISTING"),
            "is_relevant_at_entry": relevant,
            "relevance_reasons": reasons,
            "is_major_wall_q95_past_only": major,
            "baseline_n_at_entry": len(past_sizes),
            "wall_relevance_status": WALL_RELEVANCE_STATUS,
            "relevance_contract": RELEVANCE_CONTRACT["status"],
        }
        out.append(row)

        # Update baseline AFTER scoring (past-only for next walls)
        past_sizes.append(init_q)
        past_notionals.append(init_notional)

    return out


def summarize_generation_vs_relevant(scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Separate churn stats from relevant walls."""
    n = len(scored)
    prices = {float(g["price"]) for g in scored}
    relevant = [g for g in scored if g.get("is_relevant_at_entry")]
    attacked = [
        g
        for g in relevant
        if float(g.get("cumulative_attributed_fills") or 0) > EPSILON
        or g.get("first_trade_touch")
    ]
    untouched = [g for g in relevant if g not in attacked and g not in []]
    untouched = [
        g
        for g in relevant
        if float(g.get("cumulative_attributed_fills") or 0) <= EPSILON and not g.get("first_trade_touch")
    ]

    # peak concurrent active generations / prices (event-time sweep)
    events: list[tuple[float, int, float]] = []
    for g in scored:
        st = _as_dt(g["start_time"]).timestamp()
        en = _as_dt(g["end_time"]).timestamp() if g.get("end_time") else _as_dt(
            g.get("start_time")
        ).timestamp() + 1e9
        events.append((st, +1, float(g["price"])))
        events.append((en, -1, float(g["price"])))
    events.sort(key=lambda x: (x[0], -x[1]))
    cur_g = 0
    peak_g = 0
    active_prices: dict[float, int] = {}
    peak_p = 0
    for _, delta, px in events:
        cur_g += delta
        peak_g = max(peak_g, cur_g)
        active_prices[px] = active_prices.get(px, 0) + delta
        if active_prices[px] <= 0:
            active_prices.pop(px, None)
        peak_p = max(peak_p, len(active_prices))

    return {
        "generation_count": n,
        "unique_price_level_count": len(prices),
        "peak_concurrent_active_generations": peak_g,
        "peak_concurrent_active_prices": peak_p,
        "relevant_wall_count": len(relevant),
        "attacked_relevant_wall_count": len(attacked),
        "untouched_relevant_wall_count": len(untouched),
        "major_wall_q95_count": sum(1 for g in scored if g.get("is_major_wall_q95_past_only")),
        "note": "generation_count must not be read as relevant_wall_count",
    }
