"""
Forecast helpers for spread-controlled cycle planning.
"""

from __future__ import annotations

from typing import Any, Optional

from bots.shared.bot_context import BotContext
from bots.shared.spread_profile import resolve_rebuy_profile


def _weighted_avg(existing_size: float, existing_avg: float, add_size: float, fill_price: float) -> Optional[float]:
    total_size = float(existing_size or 0) + float(add_size or 0)
    if total_size <= 0:
        return None
    existing_notional = float(existing_size or 0) * float(existing_avg or 0)
    add_notional = float(add_size or 0) * float(fill_price or 0)
    return (existing_notional + add_notional) / total_size


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except Exception:
        return default


def plan_short_cycle_profile(
    *,
    rebuy_profile: Any,
    spread_zones: Any,
    base_hedge_ratio: float,
    cycle_index: int,
    initial_short_usdt: float,
    burn_count_after: int,
    burns_before_rebuy: int,
    tp_price: float,
    short_size_after_burn: float,
    short_avg_after_burn: float,
    projected_spread_trigger_pct: float = 3.0,
) -> Optional[dict[str, Any]]:
    if burn_count_after < burns_before_rebuy:
        return None
    if tp_price <= 0 or short_size_after_burn <= 0 or short_avg_after_burn <= 0:
        return None

    cycle_rf, cycle_hr, _, cycle_idx = resolve_rebuy_profile(
        rebuy_profile,
        base_hedge_ratio,
        cycle_index,
        spread_pct=None,
        zones=None,
    )

    cycle_target_short = max((initial_short_usdt * cycle_rf) / tp_price, 0.0)
    cycle_missing_short = max(cycle_target_short - short_size_after_burn, 0.0)
    cycle_short_avg = _weighted_avg(short_size_after_burn, short_avg_after_burn, cycle_missing_short, tp_price)
    cycle_long_avg = tp_price
    cycle_spread = BotContext.compute_ls_spread(cycle_long_avg, cycle_short_avg)
    cycle_spread_pct = abs(cycle_spread * 100.0) if cycle_spread is not None else None

    selected_rf = cycle_rf
    selected_hr = cycle_hr
    selected_zone = None
    selected_idx = cycle_idx
    selection_mode = "cycle_only"

    if cycle_spread_pct is not None and cycle_spread_pct >= projected_spread_trigger_pct:
        selected_rf, selected_hr, selected_zone, selected_idx = resolve_rebuy_profile(
            rebuy_profile,
            base_hedge_ratio,
            cycle_index,
            spread_pct=cycle_spread_pct,
            zones=spread_zones if isinstance(spread_zones, dict) else None,
        )
        selection_mode = "projected_spread_override"

    selected_target_short = max((initial_short_usdt * selected_rf) / tp_price, 0.0)
    selected_missing_short = max(selected_target_short - short_size_after_burn, 0.0)
    selected_short_avg = _weighted_avg(short_size_after_burn, short_avg_after_burn, selected_missing_short, tp_price)
    selected_long_avg = tp_price
    selected_spread = BotContext.compute_ls_spread(selected_long_avg, selected_short_avg)
    selected_spread_pct = abs(selected_spread * 100.0) if selected_spread is not None else None
    selected_target_long = selected_target_short * selected_hr

    return {
        "burn_count_after": burn_count_after,
        "burns_before_rebuy": burns_before_rebuy,
        "selection_mode": selection_mode,
        "trigger_spread_pct": projected_spread_trigger_pct,
        "cycle_profile_idx": cycle_idx,
        "cycle_rebuy_factor": cycle_rf,
        "cycle_hedge_ratio": cycle_hr,
        "cycle_projected_spread_pct": cycle_spread_pct,
        "selected_profile_idx": selected_idx,
        "selected_zone": selected_zone,
        "selected_rebuy_factor": selected_rf,
        "selected_hedge_ratio": selected_hr,
        "selected_projected_spread_pct": selected_spread_pct,
        "target_short_size": selected_target_short,
        "target_long_size": selected_target_long,
        "missing_short_size": selected_missing_short,
        "locked_for_cycle": True,
    }


def plan_long_cycle_profile(
    *,
    rebuy_profile: Any,
    spread_zones: Any,
    base_hedge_ratio: float,
    cycle_index: int,
    initial_long_usdt: float,
    burn_count_after: int,
    burns_before_rebuy: int,
    tp_price: float,
    long_size_after_burn: float,
    long_avg_after_burn: float,
    projected_spread_trigger_pct: float = 3.0,
) -> Optional[dict[str, Any]]:
    if burn_count_after < burns_before_rebuy:
        return None
    if tp_price <= 0 or long_size_after_burn <= 0 or long_avg_after_burn <= 0:
        return None

    cycle_rf, cycle_hr, _, cycle_idx = resolve_rebuy_profile(
        rebuy_profile,
        base_hedge_ratio,
        cycle_index,
        spread_pct=None,
        zones=None,
    )

    cycle_target_long = max((initial_long_usdt * cycle_rf) / tp_price, 0.0)
    cycle_missing_long = max(cycle_target_long - long_size_after_burn, 0.0)
    cycle_long_avg = _weighted_avg(long_size_after_burn, long_avg_after_burn, cycle_missing_long, tp_price)
    cycle_short_avg = tp_price
    cycle_spread = BotContext.compute_ls_spread(cycle_long_avg, cycle_short_avg)
    cycle_spread_pct = abs(cycle_spread * 100.0) if cycle_spread is not None else None

    selected_rf = cycle_rf
    selected_hr = cycle_hr
    selected_zone = None
    selected_idx = cycle_idx
    selection_mode = "cycle_only"

    if cycle_spread_pct is not None and cycle_spread_pct >= projected_spread_trigger_pct:
        selected_rf, selected_hr, selected_zone, selected_idx = resolve_rebuy_profile(
            rebuy_profile,
            base_hedge_ratio,
            cycle_index,
            spread_pct=cycle_spread_pct,
            zones=spread_zones if isinstance(spread_zones, dict) else None,
        )
        selection_mode = "projected_spread_override"

    selected_target_long = max((initial_long_usdt * selected_rf) / tp_price, 0.0)
    selected_missing_long = max(selected_target_long - long_size_after_burn, 0.0)
    selected_long_avg = _weighted_avg(long_size_after_burn, long_avg_after_burn, selected_missing_long, tp_price)
    selected_short_avg = tp_price
    selected_spread = BotContext.compute_ls_spread(selected_long_avg, selected_short_avg)
    selected_spread_pct = abs(selected_spread * 100.0) if selected_spread is not None else None
    selected_target_short = selected_target_long * selected_hr

    return {
        "burn_count_after": burn_count_after,
        "burns_before_rebuy": burns_before_rebuy,
        "selection_mode": selection_mode,
        "trigger_spread_pct": projected_spread_trigger_pct,
        "cycle_profile_idx": cycle_idx,
        "cycle_rebuy_factor": cycle_rf,
        "cycle_hedge_ratio": cycle_hr,
        "cycle_projected_spread_pct": cycle_spread_pct,
        "selected_profile_idx": selected_idx,
        "selected_zone": selected_zone,
        "selected_rebuy_factor": selected_rf,
        "selected_hedge_ratio": selected_hr,
        "selected_projected_spread_pct": selected_spread_pct,
        "target_long_size": selected_target_long,
        "target_short_size": selected_target_short,
        "missing_long_size": selected_missing_long,
        "locked_for_cycle": True,
    }
