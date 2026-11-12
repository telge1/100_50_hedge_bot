"""Causal entry-level touch on 5m candles (no lookahead)."""

from __future__ import annotations

from typing import Any


def entry_level_touch(
    candle: dict[str, Any],
    entry: float,
    tolerance_pct: float = 0.0,
) -> tuple[bool, str]:
    """Long planned entry touched if price trades through entry within bar."""
    lo = float(candle["low"])
    hi = float(candle["high"])
    if tolerance_pct > 0:
        band = entry * tolerance_pct / 100.0
        lo_b, hi_b = entry - band, entry + band
        if hi >= lo_b and lo <= hi_b:
            return True, f"band_touch tol={tolerance_pct}%"
        return False, "outside_band"
    if lo <= entry <= hi:
        return True, "interval_touch"
    return False, "no_touch"


def box_active_at(box_start: int, box_end: int, bar_open_ts: int) -> bool:
    return box_start <= bar_open_ts <= box_end
