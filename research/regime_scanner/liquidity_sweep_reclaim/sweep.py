"""Sweep detection and penetration classes."""

from __future__ import annotations

from typing import Any

import numpy as np

from research.regime_scanner.liquidity_sweep_reclaim.config import (
    LSRConfig,
    default_config,
    penetration_min_atr,
)


def _finite(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return v


def measure_sweep(
    *,
    side: str,
    level: float,
    high: float,
    low: float,
    open_: float,
    close: float,
    atr: float,
    volume: float | None = None,
    volume_ma: float | None = None,
) -> dict[str, Any] | None:
    """Return sweep metrics if intrabar trade beyond level; else None."""
    atr_v = max(float(atr), 1e-12)
    if side == "long":
        if not (low < level):
            return None
        extreme = float(low)
        penetration = level - extreme
        wick_beyond = level - extreme
        body_low = min(open_, close)
        # wick portion beyond level relative to full candle range
    elif side == "short":
        if not (high > level):
            return None
        extreme = float(high)
        penetration = extreme - level
        wick_beyond = extreme - level
        body_low = max(open_, close)
    else:
        raise ValueError(f"unknown side: {side}")

    candle_range = max(high - low, 1e-12)
    penetration_atr = penetration / atr_v
    penetration_pct = penetration / max(abs(level), 1e-12) * 100.0
    wick_beyond_pct = wick_beyond / candle_range * 100.0
    body = abs(close - open_)
    vol_ratio = None
    if volume is not None and volume_ma is not None and volume_ma > 0:
        vol_ratio = float(volume) / float(volume_ma)

    # close location in candle [0=low, 1=high]
    close_loc = (close - low) / candle_range
    if side == "long":
        directional_wick_ratio = wick_beyond / candle_range
    else:
        directional_wick_ratio = wick_beyond / candle_range

    return {
        "sweep_extreme": extreme,
        "penetration": penetration,
        "penetration_atr": float(penetration_atr),
        "penetration_pct": float(penetration_pct),
        "wick_beyond_level_pct": float(wick_beyond_pct),
        "wick_beyond_level": float(wick_beyond),
        "candle_range_atr": float(candle_range / atr_v),
        "sweep_body_atr": float(body / atr_v),
        "close_location": float(close_loc),
        "directional_wick_ratio": float(directional_wick_ratio),
        "volume_ratio": vol_ratio,
        "oversized_break": bool(penetration_atr > default_config().max_penetration_atr),
    }


def qualifies_penetration(p_class: str, penetration_atr: float, cfg: LSRConfig | None = None) -> bool:
    c = cfg or default_config()
    if penetration_atr <= 0:
        return False
    if penetration_atr > c.max_penetration_atr:
        return False
    return float(penetration_atr) >= penetration_min_atr(p_class, c)


def same_candle_reclaim_close(*, side: str, level: float, close: float) -> bool:
    if side == "long":
        return close > level
    return close < level
