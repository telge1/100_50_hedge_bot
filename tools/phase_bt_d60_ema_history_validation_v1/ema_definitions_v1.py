"""EMA definitions for BT-D60 (research-only; documents Research Charts vs BT-D59)."""

from __future__ import annotations

from typing import Literal, Sequence

EmaPathId = Literal["trp_research_charts", "bt_d59_first_close", "liquidity_context_engine"]

EMA_PERIODS = (9, 20, 59, 200)

TRP_RESEARCH_CHARTS_DOC = (
    "TRP indicators.ema.ema: SMA of first `period` closes seeds EMA at index period-1; "
    "k=2/(period+1); ema[i]=(value[i]-ema[i-1])*k+ema[i-1]; earlier indices None. "
    "Source: trading_research_platform/indicators/ema.py (Research Charts server path)."
)

BT_D59_FIRST_CLOSE_DOC = (
    "BT-D54/D59: recursive α=2/(n+1) from bar 0 with first close as seed (no SMA warmup). "
    "Source: tools/phase_bt_d59_market_regime_pool_evolution_v1/ema_research_v1.py."
)

LIQUIDITY_CONTEXT_DOC = (
    "liquidity_context_engine_v1.ema_series: identical SMA-seed semantics to TRP. "
    "Source: research_charts/liquidity_context_engine_v1.py."
)


def ema_trp_sma_seed(closes: Sequence[float], period: int) -> list[float | None]:
    """Research Charts / TRP canonical path."""
    if period < 1:
        raise ValueError("period")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period:
        return out
    seed = sum(float(v) for v in closes[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        value = float(closes[i])
        prev = (value - prev) * k + prev
        out[i] = prev
    return out


def ema_bt_d59_first_close(closes: Sequence[float], period: int) -> list[float | None]:
    """Legacy BT-D59 path (preliminary until BT-D60 pass)."""
    if period <= 0:
        raise ValueError("period")
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = []
    prev: float | None = None
    for c in closes:
        if prev is None:
            prev = float(c)
        else:
            prev = alpha * float(c) + (1.0 - alpha) * prev
        out.append(prev)
    return out


def ema_liquidity_context(closes: Sequence[float], period: int) -> list[float | None]:
    """Alias check path (must match TRP)."""
    return ema_trp_sma_seed(closes, period)


def ema_series_for_path(path: EmaPathId, closes: Sequence[float], period: int) -> list[float | None]:
    if path == "trp_research_charts":
        return ema_trp_sma_seed(closes, period)
    if path == "bt_d59_first_close":
        return ema_bt_d59_first_close(closes, period)
    if path == "liquidity_context_engine":
        return ema_liquidity_context(closes, period)
    raise ValueError(path)


def ema_at_index(series: list[float | None], idx: int) -> float | None:
    if idx < 0 or idx >= len(series):
        return None
    v = series[idx]
    return float(v) if v is not None else None
