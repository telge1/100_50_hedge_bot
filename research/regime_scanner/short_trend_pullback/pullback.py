"""Upward pullback after bearish impulse (causal)."""

from __future__ import annotations

import math
from typing import Any, Mapping

from research.regime_scanner.short_trend_pullback.config import STPConfig
from research.regime_scanner.short_trend_pullback.models import ImpulseState, PullbackState


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


def pullback_begin(row: Mapping[str, Any], impulse: ImpulseState) -> bool:
    """First upward counter-move after impulse low: close > impulse end low and bullish/micro high."""
    close = _f(row, "close")
    if close is None:
        return False
    if close <= impulse.end_price:
        return False
    # controlled: still below protected high
    if impulse.protected_high is not None and close >= impulse.protected_high:
        return False
    bullish = close > float(row["open"])
    micro = bool(row.get("new_micro_high")) or bool(row.get("arm_edge_internal_bull"))
    reclaim_ema = False
    e9 = _f(row, "ema_9")
    e20 = _f(row, "ema_20")
    if e9 is not None and close >= e9:
        reclaim_ema = True
    if e20 is not None and close >= e20:
        reclaim_ema = True
    return bool(bullish or micro or reclaim_ema)


def update_pullback(
    pb: PullbackState,
    row: Mapping[str, Any],
    bar_i: int,
    impulse: ImpulseState,
    *,
    cfg: STPConfig,
) -> PullbackState:
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    if high >= pb.high:
        pb.high = high
        pb.high_bar = bar_i
        pb.low_after_high = low
        pb.low_after_high_bar = bar_i
    else:
        if pb.low_after_high is None or low < pb.low_after_high:
            pb.low_after_high = low
            pb.low_after_high_bar = bar_i
    pb.end_bar = bar_i
    pb.bars = bar_i - pb.start_bar + 1
    impulse_span = max(1e-12, impulse.start_price - impulse.end_price)
    pb.retracement = max(0.0, (pb.high - impulse.end_price) / impulse_span)
    pb.return_pct = (pb.high / impulse.end_price - 1.0) * 100.0 if impulse.end_price else 0.0
    atr = impulse.atr if impulse.atr > 0 else (_f(row, "atr_14", "atr") or 1.0)
    pb.atr_move = (pb.high - impulse.end_price) / atr
    path = abs(pb.high - impulse.end_price)
    pb.efficiency = min(1.0, path / max(path, atr * pb.bars * 0.1))
    pb.volume_sum += float(row.get("volume") or 0.0)
    if impulse.volume_sum > 0:
        pb.volume_ratio = pb.volume_sum / impulse.volume_sum
    if bool(row.get("arm_edge_internal_bull")) or bool(row.get("internal_bos_up")):
        pb.internal_bull_bos = True
    if bool(row.get("arm_edge_choch_bull")) or str(row.get("choch_side") or "") == "up":
        pb.external_bull_choch = True
    ph = impulse.protected_high
    if ph is not None and ph > 0:
        pb.dist_protected_high_pct = (ph - close) / ph * 100.0
    e20 = _f(row, "ema_20")
    e59 = _f(row, "ema_59")
    if e20:
        pb.dist_ema20_pct = (close - e20) / e20 * 100.0
    if e59:
        pb.dist_ema59_pct = (close - e59) / e59 * 100.0
    return pb


def pullback_invalid(
    pb: PullbackState,
    row: Mapping[str, Any],
    impulse: ImpulseState,
    *,
    cfg: STPConfig,
) -> str | None:
    if pb.external_bull_choch:
        return "bullish_external_choch"
    ph = impulse.protected_high
    close = _f(row, "close")
    if ph is not None and close is not None and close > ph:
        return "protected_high_broken"
    if pb.bars > cfg.max_pullback_bars:
        return "pullback_too_long"
    if pb.retracement > cfg.max_retracement:
        return "pullback_too_deep"
    return None


def new_pullback_state(bar_i: int, row: Mapping[str, Any], impulse: ImpulseState) -> PullbackState:
    high = float(row["high"])
    return PullbackState(
        start_bar=bar_i,
        end_bar=bar_i,
        high=high,
        high_bar=bar_i,
        low_after_high=float(row["low"]),
        low_after_high_bar=bar_i,
        bars=1,
        return_pct=0.0,
        atr_move=0.0,
        retracement=0.0,
        efficiency=0.0,
        volume_sum=float(row.get("volume") or 0.0),
        volume_ratio=None,
        internal_bull_bos=bool(row.get("arm_edge_internal_bull")),
        external_bull_choch=bool(row.get("arm_edge_choch_bull")),
        dist_protected_high_pct=None,
        dist_ema20_pct=None,
        dist_ema59_pct=None,
    )
