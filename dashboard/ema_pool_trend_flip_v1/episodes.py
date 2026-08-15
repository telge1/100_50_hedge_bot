"""One trade per uninterrupted stochastic extreme episode on the signal timeframe."""

from __future__ import annotations

from typing import Sequence

from .config import STOCH_D_PERIOD, STOCH_K_PERIOD, STOCH_OVERBOUGHT, STOCH_OVERSOLD, STOCH_SMOOTH


def _sma(vals: Sequence[float | None], period: int) -> list[float | None]:
    out: list[float | None] = []
    buf: list[float] = []
    for v in vals:
        if v is None:
            buf = []
            out.append(None)
            continue
        buf.append(float(v))
        if len(buf) > period:
            buf.pop(0)
        if len(buf) < period:
            out.append(None)
        else:
            out.append(sum(buf) / float(period))
    return out


def stochastic_k(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float | None]:
    n = len(closes)
    raw: list[float | None] = [None] * n
    p = STOCH_K_PERIOD
    for i in range(n):
        if i + 1 < p:
            continue
        window_h = max(highs[i + 1 - p : i + 1])
        window_l = min(lows[i + 1 - p : i + 1])
        den = window_h - window_l
        if den <= 0:
            raw[i] = 50.0
        else:
            raw[i] = 100.0 * (closes[i] - window_l) / den
    k1 = _sma(raw, STOCH_SMOOTH)
    k = _sma(k1, STOCH_D_PERIOD)
    return k


def episode_ids(k_values: Sequence[float | None], *, for_short: bool) -> list[int | None]:
    """Increment when the series leaves the extreme and later re-enters."""
    ids: list[int | None] = []
    current: int | None = None
    next_id = 1
    in_zone = False
    for k in k_values:
        if k is None:
            ids.append(None)
            continue
        zone = (k >= STOCH_OVERBOUGHT) if for_short else (k <= STOCH_OVERSOLD)
        if zone and not in_zone:
            current = next_id
            next_id += 1
            in_zone = True
        elif not zone:
            in_zone = False
            current = None
        ids.append(current if in_zone else None)
    return ids
