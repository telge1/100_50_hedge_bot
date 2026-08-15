"""Downward impulse detection (causal, not every red candle)."""

from __future__ import annotations

import math
from typing import Any, Mapping

from research.regime_scanner.short_trend_pullback.config import STPConfig
from research.regime_scanner.short_trend_pullback.models import ImpulseState


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


def impulse_start_event(row: Mapping[str, Any]) -> bool:
    """True impulse start: bearish external BOS or major flip to bearish."""
    return bool(row.get("arm_edge_external_bear")) or bool(row.get("arm_edge_major_bear"))


def build_impulse_from_bars(
    frame_rows: list[Mapping[str, Any]],
    *,
    start_i: int,
    end_i: int,
    cfg: STPConfig,
) -> ImpulseState | None:
    if end_i < start_i:
        return None
    bars = end_i - start_i + 1
    if bars < cfg.min_impulse_bars or bars > cfg.max_impulse_bars:
        return None
    start = frame_rows[start_i]
    end = frame_rows[end_i]
    start_px = _f(start, "high")  # impulse from local high area
    # use open of start as reference high proxy if needed
    if start_px is None:
        start_px = _f(start, "open")
    end_px = _f(end, "low")
    if start_px is None or end_px is None or start_px <= 0:
        return None
    # track actual max high / min low in window
    hi = max(float(frame_rows[j]["high"]) for j in range(start_i, end_i + 1))
    lo = min(float(frame_rows[j]["low"]) for j in range(start_i, end_i + 1))
    atr = _f(end, "atr_14", "atr") or _f(start, "atr_14", "atr")
    if atr is None or atr <= 0:
        return None
    move = hi - lo
    atr_move = move / atr
    if atr_move < cfg.min_impulse_atr:
        return None
    # directional: must be net down
    ret = (lo / hi - 1.0) * 100.0
    if ret >= 0:
        return None
    # DI confirmation preferred but not mandatory if BOS present
    vol = sum(float(frame_rows[j].get("volume") or 0.0) for j in range(start_i, end_i + 1))
    # efficiency: net move / path range sum
    path = 0.0
    for j in range(start_i, end_i + 1):
        path += abs(float(frame_rows[j]["high"]) - float(frame_rows[j]["low"]))
    eff = (move / path) if path > 0 else 0.0
    ph = _f(end, "protected_high")
    bos_ts = start.get("timestamp") if impulse_start_event(start) else end.get("timestamp")
    return ImpulseState(
        start_bar=start_i,
        end_bar=end_i,
        start_price=hi,
        end_price=lo,
        high_at_start=hi,
        bars=bars,
        return_pct=ret,
        atr=atr,
        atr_move=atr_move,
        efficiency=eff,
        volume_sum=vol,
        bos_timestamp=bos_ts,
        protected_high=ph,
    )


def protected_high_intact(row: Mapping[str, Any], ph: float | None) -> bool:
    if ph is None:
        return False
    close = _f(row, "close")
    high = _f(row, "high")
    if close is None or high is None:
        return False
    # broken if close above protected high
    return close <= ph
