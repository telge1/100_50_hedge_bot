"""Bearish context predicates B1/B2/B3 (frozen, no threshold search)."""

from __future__ import annotations

import math
from typing import Any, Mapping

import pandas as pd

from research.regime_scanner.short_trend_pullback.config import STPConfig


def _f(row: Mapping[str, Any], *keys: str) -> float | None:
    for k in keys:
        if k not in row:
            continue
        try:
            v = float(row[k])
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            return v
    return None


def _slope(row: Mapping[str, Any], ema: str, lookback: int) -> float | None:
    # prefer precomputed; else None (caller may use frame-level)
    return _f(row, f"{ema}_slope_{lookback}", f"{ema}_slope_{lookback}_atr")


def is_range_or_transition(row: Mapping[str, Any]) -> bool:
    """Do not trade range/transition as bearish setup context."""
    maj = row.get("major_direction")
    try:
        m = int(maj) if maj is not None and pd.notna(maj) else 0
    except (TypeError, ValueError):
        m = 0
    if m == 0:
        return True
    # soft: ADX very low → range-like (diagnostic mark only used as soft skip when maj==0)
    return False


def context_b1(row: Mapping[str, Any], *, cfg: STPConfig, recent_above_ema200_share: float | None) -> bool:
    """EMA mid/macro bearish.

    * EMA20 < EMA59 < EMA200
    * EMA20 and EMA59 slopes not clearly rising (slope_3 <= 0)
    * price not persistently above EMA200 (share of last N closes above <= max)
    """
    e20 = _f(row, "ema_20", "ema20")
    e59 = _f(row, "ema_59", "ema59")
    e200 = _f(row, "ema_200", "ema200")
    if e20 is None or e59 is None or e200 is None:
        return False
    if not (e20 < e59 < e200):
        return False
    s20 = _slope(row, "ema_20", cfg.slope_lookback)
    s59 = _slope(row, "ema_59", cfg.slope_lookback)
    # if slopes missing, require close below ema200 as weaker proxy
    if s20 is not None and s20 > 0:
        return False
    if s59 is not None and s59 > 0:
        return False
    if recent_above_ema200_share is not None and recent_above_ema200_share > cfg.ema200_above_share_max:
        return False
    close = _f(row, "close")
    if close is not None and close > e200:
        # single close above is allowed only if not persistent; already gated by share
        pass
    return True


def context_b2(row: Mapping[str, Any]) -> bool:
    """Bearish structure: major bearish, protected high present, no bullish external CHOCH."""
    try:
        maj = int(row.get("major_direction") or 0)
    except (TypeError, ValueError):
        maj = 0
    if maj >= 0:
        return False
    ph = _f(row, "protected_high")
    if ph is None:
        return False
    choch = str(row.get("choch_side") or "").lower()
    if choch == "up":
        return False
    if bool(row.get("arm_edge_choch_bull")):
        return False
    return True


def context_b3(row: Mapping[str, Any], *, cfg: STPConfig, recent_above_ema200_share: float | None) -> bool:
    return context_b1(row, cfg=cfg, recent_above_ema200_share=recent_above_ema200_share) and context_b2(row)


def context_ok(
    context: str,
    row: Mapping[str, Any],
    *,
    cfg: STPConfig,
    recent_above_ema200_share: float | None,
) -> bool:
    if is_range_or_transition(row) and context in ("B1", "B2", "B3"):
        # major==0 → not eligible for B2/B3; B1 can still pass on EMA alone
        if context in ("B2", "B3"):
            return False
    if context == "B1":
        return context_b1(row, cfg=cfg, recent_above_ema200_share=recent_above_ema200_share)
    if context == "B2":
        return context_b2(row)
    if context == "B3":
        return context_b3(row, cfg=cfg, recent_above_ema200_share=recent_above_ema200_share)
    raise KeyError(context)
