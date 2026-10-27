"""Continuous market-regime measurements (research-only, no trading thresholds)."""

from __future__ import annotations

from typing import Any

from ema_research_v1 import EMA_PERIODS, count_ema9_20_crosses, slope_pct


def _swing_structure(
    highs: list[float], lows: list[float], lookback: int
) -> dict[str, Any]:
    if len(highs) < 3:
        return {"lower_high_count": None, "lower_low_count": None, "higher_high_count": None}
    seg_h = highs[-lookback:] if len(highs) >= lookback else highs
    seg_l = lows[-lookback:] if len(lows) >= lookback else lows
    lhs = lls = hhs = 0
    for i in range(2, len(seg_h)):
        if seg_h[i] < seg_h[i - 1] < seg_h[i - 2]:
            lhs += 1
        if seg_l[i] < seg_l[i - 1] < seg_l[i - 2]:
            lls += 1
        if seg_h[i] > seg_h[i - 1] > seg_h[i - 2]:
            hhs += 1
    return {"lower_high_count": lhs, "lower_low_count": lls, "higher_high_count": hhs}


def compute_regime_features_at_close(
    *,
    bar: str,
    bars: list[str],
    prices: dict[str, dict[str, float]],
    emas: dict[int, dict[str, float]],
) -> dict[str, Any]:
    idx = bars.index(bar)
    past_bars = bars[: idx + 1]
    closes = [float(prices[b]["c"]) for b in past_bars]
    highs = [float(prices[b]["h"]) for b in past_bars]
    lows = [float(prices[b]["l"]) for b in past_bars]
    close = closes[-1]

    e9, e20, e59, e200 = emas[9][bar], emas[20][bar], emas[59][bar], emas[200][bar]
    order_stack = e9 < e20 < e59 < e200

    feats: dict[str, Any] = {
        "decision_close_utc": bar,
        "close": close,
        "ema9": e9,
        "ema20": e20,
        "ema59": e59,
        "ema200": e200,
        "close_below_ema9": close < e9,
        "close_below_ema20": close < e20,
        "close_below_ema59": close < e59,
        "close_below_ema200": close < e200,
        "ema9_lt_20_lt_59_lt_200": order_stack,
        "ema9_minus_ema20_pct": (e9 - e20) / e20 * 100.0 if e20 else None,
        "ema20_minus_ema59_pct": (e20 - e59) / e59 * 100.0 if e59 else None,
        "ema59_minus_ema200_pct": (e59 - e200) / e200 * 100.0 if e200 else None,
        "ema9_slope_3b_pct": slope_pct(emas[9], bar, bars, 3),
        "ema9_slope_6b_pct": slope_pct(emas[9], bar, bars, 6),
        "ema20_slope_6b_pct": slope_pct(emas[20], bar, bars, 6),
        "ema59_slope_12b_pct": slope_pct(emas[59], bar, bars, 12),
        "ema200_slope_12b_pct": slope_pct(emas[200], bar, bars, 12),
        "ema9_20_cross_count_past_only": count_ema9_20_crosses(bars, emas[9], emas[20], idx),
    }

    win = 24
    seg_c = closes[-win:] if len(closes) >= win else closes
    seg_h = highs[-win:] if len(highs) >= win else highs
    seg_l = lows[-win:] if len(lows) >= win else lows
    rng = max(seg_h) - min(seg_l) if seg_h else 0.0
    net = seg_c[-1] - seg_c[0] if len(seg_c) > 1 else 0.0
    path = sum(abs(seg_c[i] - seg_c[i - 1]) for i in range(1, len(seg_c)))
    feats["range_24b_pct"] = (rng / close * 100.0) if close else None
    feats["net_move_24b_pct"] = (net / seg_c[0] * 100.0) if seg_c and seg_c[0] else None
    feats["chop_ratio_24b"] = (path / abs(net)) if net != 0 else None
    feats.update(_swing_structure(highs, lows, 24))
    return feats
