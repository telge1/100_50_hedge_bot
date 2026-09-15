"""Neutral price-phase detection for manual-window analysis."""

from __future__ import annotations

from typing import Any, Sequence


def detect_neutral_phases(minute_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not minute_rows:
        return []
    phases: list[dict[str, Any]] = []
    # Simple segmentation by range vs move
    chunk: list[dict[str, Any]] = []
    for row in minute_rows:
        chunk.append(row)
        if len(chunk) < 5:
            continue
        # flush every 5 minutes as a descriptive block
        o = float(chunk[0]["o"])
        c = float(chunk[-1]["c"])
        hi = max(float(x["h"]) for x in chunk)
        lo = min(float(x["l"]) for x in chunk)
        move = c - o
        rng = hi - lo
        if rng < 20:
            name = "compression"
        elif abs(move) >= 0.7 * rng and move > 0:
            name = "expansion_up"
        elif abs(move) >= 0.7 * rng and move < 0:
            name = "expansion_down"
        elif abs(move) < 0.3 * rng:
            name = "fade_or_balance"
        else:
            name = "follow_through_mixed"
        phases.append(
            {
                "name": name,
                "start_utc": chunk[0].get("minute_utc"),
                "end_utc": chunk[-1].get("minute_utc"),
                "move": move,
                "range": rng,
                "delta": sum(float(x.get("delta") or 0.0) for x in chunk),
            }
        )
        chunk = []
    return phases


def detect_important_levels(minute_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not minute_rows:
        return []
    highs = sorted({round(float(r["h"]), 2) for r in minute_rows}, reverse=True)
    lows = sorted({round(float(r["l"]), 2) for r in minute_rows})
    levels = []
    if highs:
        levels.append({"kind": "local_high", "price": highs[0], "role": "observed"})
    if lows:
        levels.append({"kind": "local_low", "price": lows[0], "role": "observed"})
    # second extremes if distinct
    if len(highs) > 1:
        levels.append({"kind": "secondary_high", "price": highs[1], "role": "observed"})
    if len(lows) > 1:
        levels.append({"kind": "secondary_low", "price": lows[1], "role": "observed"})
    return levels
