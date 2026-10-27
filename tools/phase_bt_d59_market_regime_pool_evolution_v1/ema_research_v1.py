"""Past-only EMA features for BT-D59 (research; matches BT-D54 initialization)."""

from __future__ import annotations

from typing import Sequence

EMA_PERIODS = (9, 20, 59, 200)


def compute_ema_series(closes: Sequence[float], period: int) -> list[float | None]:
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


def ema_map_for_bars(bars: list[str], prices: dict[str, dict[str, float]]) -> dict[int, dict[str, float]]:
    closes = [float(prices[b]["c"]) for b in bars]
    ema: dict[int, dict[str, float]] = {p: {} for p in EMA_PERIODS}
    for period in EMA_PERIODS:
        series = compute_ema_series(closes, period)
        for i, bar in enumerate(bars):
            ema[period][bar] = series[i]  # type: ignore[assignment]
    return ema


def slope_pct(ema_by_bar: dict[str, float], bar: str, bars: list[str], lookback_bars: int) -> float | None:
    idx = bars.index(bar)
    if idx < lookback_bars:
        return None
    prev_bar = bars[idx - lookback_bars]
    a, b = ema_by_bar[bar], ema_by_bar[prev_bar]
    if b == 0:
        return None
    return (a - b) / b * 100.0


def count_ema9_20_crosses(bars: list[str], ema9: dict[str, float], ema20: dict[str, float], upto_idx: int) -> int:
    crosses = 0
    for i in range(1, upto_idx + 1):
        b0, b1 = bars[i - 1], bars[i]
        d0 = ema9[b0] - ema20[b0]
        d1 = ema9[b1] - ema20[b1]
        if d0 == 0 or d1 == 0:
            continue
        if (d0 > 0 and d1 < 0) or (d0 < 0 and d1 > 0):
            crosses += 1
    return crosses
