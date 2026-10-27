"""Research-only regime variant flags A–F (no trading thresholds)."""

from __future__ import annotations

from typing import Any


def variant_flags(regime: dict[str, Any], pool: dict[str, Any]) -> dict[str, Any]:
    """Boolean research variants — descriptive, not entry rules."""
    a = bool(regime.get("close_below_ema200"))
    b = bool(regime.get("ema9_lt_20_lt_59_lt_200"))
    c = b and (
        (regime.get("ema59_slope_12b_pct") or 0) < 0
        and (regime.get("ema200_slope_12b_pct") or 0) < 0
    )
    d = c and regime.get("net_move_24b_pct") is not None and regime.get("chop_ratio_24b") is not None
    if d:
        d = (regime["net_move_24b_pct"] or 0) < 0 and (regime["chop_ratio_24b"] or 99) < 8.0
    e = d and pool.get("gap_to_next_upper_pct") is not None and (pool["gap_to_next_upper_pct"] or 0) >= 0.5
    e = e and (pool.get("upper_bands_between_entry_and_next") or 0) == 0
    f = e and (pool.get("local_stack_density_0p35pct") or 0) <= 2
    f_growth_warn = (pool.get("local_stack_density_0p35pct") or 0) >= 3 or bool(
        pool.get("origin_ids_added")
    )
    return {
        "variant_A_close_below_ema200": a,
        "variant_B_ema_stack_bearish": b,
        "variant_C_bearish_stack_falling_59_200": c,
        "variant_D_directional_not_chop": d,
        "variant_E_isolated_upper_gap": e,
        "variant_F_E_without_stack_growth_warn": f,
        "research_stack_growth_warning": f_growth_warn,
    }
