"""EMA59 / EMA200 multi-horizon slope research (causal, read-only)."""

from __future__ import annotations

from typing import Any

from ema_research_v1 import count_ema9_20_crosses, slope_pct

SLOPE_HORIZONS = (6, 12, 24)


def ema_slopes_at_close(
    *,
    bar: str,
    bars: list[str],
    ema59: dict[str, float],
    ema200: dict[str, float],
) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for h in SLOPE_HORIZONS:
        out[f"ema59_slope_{h}b_pct"] = slope_pct(ema59, bar, bars, h)
        out[f"ema200_slope_{h}b_pct"] = slope_pct(ema200, bar, bars, h)
    return out


def research_slope_composites(
    slopes: dict[str, float | None],
    *,
    ema9_20_crosses_past: int | None,
    net_move_24b_pct: float | None,
    chop_ratio_24b: float | None,
    stack_growth_warning: bool,
    local_stack_density: int | None,
) -> dict[str, Any]:
    """
    Descriptive research flags — no production thresholds.
    Flattening EMA59: recent 6b slope less negative than 12b slope (momentum loss).
    """
    s59_6 = slopes.get("ema59_slope_6b_pct")
    s59_12 = slopes.get("ema59_slope_12b_pct")
    s59_24 = slopes.get("ema59_slope_24b_pct")
    s200_6 = slopes.get("ema200_slope_6b_pct")
    s200_12 = slopes.get("ema200_slope_12b_pct")
    s200_24 = slopes.get("ema200_slope_24b_pct")

    ema59_flattening_6v12 = (
        s59_6 is not None and s59_12 is not None and s59_6 > s59_12
    )
    ema59_flattening_6v24 = (
        s59_6 is not None and s59_24 is not None and s59_6 > s59_24
    )
    ema59_slope_non_negative_6b = s59_6 is not None and s59_6 >= 0
    ema200_falling_6b = s200_6 is not None and s200_6 < 0
    ema200_falling_12b = s200_12 is not None and s200_12 < 0
    ema200_falling_24b = s200_24 is not None and s200_24 < 0

    combo_flat59_fall200_12 = ema59_flattening_6v12 and ema200_falling_12b
    combo_flat59_fall200_24 = ema59_flattening_6v24 and ema200_falling_24b
    combo_nonneg59_fall200_12 = ema59_slope_non_negative_6b and ema200_falling_12b

    # Continuous context exported separately — no chop/magnitude thresholds here.
    dense_upper_context = stack_growth_warning

    return {
        "ema59_flattening_6v12": ema59_flattening_6v12,
        "ema59_flattening_6v24": ema59_flattening_6v24,
        "ema59_slope_non_negative_6b": ema59_slope_non_negative_6b,
        "ema200_falling_6b": ema200_falling_6b,
        "ema200_falling_12b": ema200_falling_12b,
        "ema200_falling_24b": ema200_falling_24b,
        "combo_flat59_fall200_12": combo_flat59_fall200_12,
        "combo_flat59_fall200_24": combo_flat59_fall200_24,
        "combo_nonneg59_fall200_12": combo_nonneg59_fall200_12,
        "net_move_24b_pct_context": net_move_24b_pct,
        "chop_ratio_24b_context": chop_ratio_24b,
        "ema9_20_crosses_last_12b_context": ema9_20_crosses_past,
        "research_combo_plus_stack": combo_flat59_fall200_12 and dense_upper_context,
        "ema59_6_minus_12_slope_pct": (
            (s59_6 - s59_12) if s59_6 is not None and s59_12 is not None else None
        ),
        "ema59_6_minus_200_12_slope_pct": (
            (s59_6 - s200_12) if s59_6 is not None and s200_12 is not None else None
        ),
    }


def crosses_in_last_n_bars(
    bars: list[str],
    ema9: dict[str, float],
    ema20: dict[str, float],
    bar: str,
    n: int,
) -> int | None:
    idx = bars.index(bar)
    start = max(0, idx - n)
    if idx - start < 2:
        return None
    return count_ema9_20_crosses(bars, ema9, ema20, idx) - count_ema9_20_crosses(
        bars, ema9, ema20, start
    )
