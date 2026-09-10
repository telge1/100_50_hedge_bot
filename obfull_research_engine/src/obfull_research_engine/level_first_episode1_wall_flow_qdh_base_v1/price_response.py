"""Price impact efficiency and uncalibrated absorption evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import EPSILON, PROGRESS_FLOOR_TICKS, TICK_SIZE


def microprice(best_bid: float, bid_size: float, best_ask: float, ask_size: float) -> float | None:
    denom = float(bid_size) + float(ask_size)
    if denom <= EPSILON:
        return None
    return (float(best_ask) * float(bid_size) + float(best_bid) * float(ask_size)) / denom


def midprice(best_bid: float, best_ask: float) -> float:
    return 0.5 * (float(best_bid) + float(best_ask))


@dataclass
class PriceResponseState:
    direction: int = 1  # +1 buy vs ask wall
    microprice_at_wall_touch: float | None = None
    midprice_at_wall_touch: float | None = None
    microprice: float | None = None
    midprice: float | None = None
    progress_bps: float = 0.0
    terminal_progress_bps: float = 0.0
    max_directional_excursion_bps_so_far: float = 0.0
    max_counter_excursion_bps_so_far: float = 0.0
    impact_efficiency_bps_per_million: float = 0.0
    spread_bps: float | None = None
    microprice_change_bps: float = 0.0
    midprice_change_bps: float = 0.0
    consumed_price_levels: float = 0.0
    absorption_pressure_raw: float | None = None
    queue_survival_under_attack: float | None = None
    refill_to_hit_ratio: float | None = None
    pull_to_hit_ratio: float | None = None
    impact_efficiency_raw: float | None = None
    absorption_ratio_normalized: float | None = None
    absorption_ratio_status: str = "NOT_CALIBRATED"


def update_price_response(
    state: PriceResponseState,
    *,
    best_bid: float,
    bid_size: float,
    best_ask: float,
    ask_size: float,
    cumulative_hit_notional_since_wall_touch: float,
    cumulative_hit_qty: float,
    cumulative_net_refill_qty: float,
    cumulative_residual_pull_qty: float,
    current_defended_band_queue: float,
    queue_at_wall_touch: float,
    wall_price: float,
    tick_size: float = TICK_SIZE,
    progress_floor_ticks: float = PROGRESS_FLOOR_TICKS,
) -> PriceResponseState:
    d = state.direction
    mp = microprice(best_bid, bid_size, best_ask, ask_size)
    mid = midprice(best_bid, best_ask)
    mp0 = state.microprice_at_wall_touch
    mid0 = state.midprice_at_wall_touch
    if mp0 is None and mp is not None:
        mp0 = mp
    if mid0 is None:
        mid0 = mid
    progress = 0.0
    micro_chg = 0.0
    mid_chg = 0.0
    if mp is not None and mp0 is not None and mp0 > EPSILON:
        raw = d * (mp - mp0) / mp0 * 10000.0
        progress = max(raw, 0.0)
        micro_chg = raw
    if mid0 is not None and mid0 > EPSILON:
        mid_chg = d * (mid - mid0) / mid0 * 10000.0
    signed = micro_chg
    max_dir = max(state.max_directional_excursion_bps_so_far, max(signed, 0.0))
    max_ctr = max(state.max_counter_excursion_bps_so_far, max(-signed, 0.0))
    hit_m = cumulative_hit_notional_since_wall_touch / 1_000_000.0
    impact = progress / (hit_m + EPSILON)
    spread = None
    if mid > EPSILON:
        spread = (best_ask - best_bid) / mid * 10000.0
    # progress in ticks from wall for absorption
    progress_ticks = 0.0
    if mp is not None:
        progress_ticks = max(d * (mp - wall_price) / tick_size, 0.0)
    abs_pressure = cumulative_hit_notional_since_wall_touch / max(progress_ticks, progress_floor_ticks)
    survival = current_defended_band_queue / (queue_at_wall_touch + EPSILON)
    refill_ratio = cumulative_net_refill_qty / (cumulative_hit_qty + EPSILON)
    pull_ratio = cumulative_residual_pull_qty / (cumulative_hit_qty + EPSILON)
    consumed = abs(best_ask - wall_price) / tick_size if d > 0 else abs(wall_price - best_bid) / tick_size
    return PriceResponseState(
        direction=d,
        microprice_at_wall_touch=mp0,
        midprice_at_wall_touch=mid0,
        microprice=mp,
        midprice=mid,
        progress_bps=progress,
        terminal_progress_bps=progress,
        max_directional_excursion_bps_so_far=max_dir,
        max_counter_excursion_bps_so_far=max_ctr,
        impact_efficiency_bps_per_million=impact,
        spread_bps=spread,
        microprice_change_bps=micro_chg,
        midprice_change_bps=mid_chg,
        consumed_price_levels=consumed,
        absorption_pressure_raw=abs_pressure,
        queue_survival_under_attack=survival,
        refill_to_hit_ratio=refill_ratio,
        pull_to_hit_ratio=pull_ratio,
        impact_efficiency_raw=impact,
        absorption_ratio_normalized=None,
        absorption_ratio_status="NOT_CALIBRATED",
    )


def price_response_to_dict(state: PriceResponseState) -> dict[str, Any]:
    return {
        "microprice": state.microprice,
        "midprice": state.midprice,
        "progress_bps": state.progress_bps,
        "terminal_progress_bps": state.terminal_progress_bps,
        "max_directional_excursion_bps_so_far": state.max_directional_excursion_bps_so_far,
        "max_counter_excursion_bps_so_far": state.max_counter_excursion_bps_so_far,
        "impact_efficiency_bps_per_million": state.impact_efficiency_bps_per_million,
        "impact_efficiency": state.impact_efficiency_bps_per_million,
        "spread_bps": state.spread_bps,
        "microprice_change_bps": state.microprice_change_bps,
        "midprice_change_bps": state.midprice_change_bps,
        "consumed_price_levels": state.consumed_price_levels,
        "absorption_pressure_raw": state.absorption_pressure_raw,
        "queue_survival_under_attack": state.queue_survival_under_attack,
        "refill_to_hit_ratio": state.refill_to_hit_ratio,
        "pull_to_hit_ratio": state.pull_to_hit_ratio,
        "impact_efficiency_raw": state.impact_efficiency_raw,
        "absorption_ratio_normalized": state.absorption_ratio_normalized,
        "absorption_ratio_status": state.absorption_ratio_status,
    }


def evidence_flags(
    *,
    hit_qty: float,
    queue_survival: float | None,
    net_refill: float,
    residual_pull: float,
    qdh_slope: float,
    persistence_ratio: float,
    progress_bps: float,
    microprice_change_bps: float,
) -> dict[str, bool]:
    """Descriptive flags only — thresholds are research defaults, not outcome-tuned."""
    return {
        "high_attack_observed": hit_qty > 0,
        "queue_survived_attack": (queue_survival or 0) > 0.5 and hit_qty > 0,
        "net_refill_observed": net_refill > 0,
        "residual_pull_observed": residual_pull > 0,
        "qdh_rising": qdh_slope > 0,
        "qdh_falling": qdh_slope < 0,
        "aggressor_accelerating": persistence_ratio > 1.0,
        "aggressor_decelerating": persistence_ratio < 1.0,
        "low_price_progress_under_attack": hit_qty > 0 and progress_bps < 1.0,
        "price_expansion_under_attack": progress_bps > 1.0,
        "microprice_reclaim_observed": microprice_change_bps < 0,
    }
