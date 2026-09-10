"""Causal refill detection (exact / nearby) within detection cut."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import pandas as pd


def detect_refills(
    level_changes: list[dict[str, Any]],
    *,
    refill_window_ms: int,
    nearby_max_bps: float,
    causal_end: pd.Timestamp,
    mid_price_hint: float | None = None,
    max_removals: int = 5000,
) -> list[dict[str, Any]]:
    causal_end = pd.to_datetime(causal_end, utc=True)
    changes = [e for e in level_changes if pd.to_datetime(e["event_time"], utc=True) < causal_end]
    removals = [e for e in changes if float(e.get("size_delta") or 0) < 0]
    adds = [e for e in changes if float(e.get("size_delta") or 0) > 0]
    # Prefer most recent removals near the cut (trigger-relevant)
    removals = sorted(removals, key=lambda e: pd.to_datetime(e["event_time"], utc=True))
    if len(removals) > max_removals:
        removals = removals[-max_removals:]

    adds_by_side: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in adds:
        adds_by_side[str(e["side"])].append(e)
    for side in adds_by_side:
        adds_by_side[side].sort(key=lambda e: pd.to_datetime(e["event_time"], utc=True))

    win = pd.Timedelta(milliseconds=refill_window_ms)
    out: list[dict[str, Any]] = []
    pointers = {side: 0 for side in adds_by_side}

    for rem in removals:
        rt = pd.to_datetime(rem["event_time"], utc=True)
        side = str(rem["side"])
        px = float(rem["price"])
        removed = abs(float(rem["notional_delta"]))
        cand_adds = adds_by_side.get(side) or []
        # advance pointer past adds before rt
        i = pointers.get(side, 0)
        while i < len(cand_adds) and pd.to_datetime(cand_adds[i]["event_time"], utc=True) < rt:
            i += 1
        pointers[side] = i
        best = None
        j = i
        while j < len(cand_adds):
            add = cand_adds[j]
            at = pd.to_datetime(add["event_time"], utc=True)
            if at >= causal_end or at - rt > win:
                break
            apx = float(add["price"])
            refilled = abs(float(add["notional_delta"]))
            mid = mid_price_hint or px
            dist_bps = abs(apx - px) / mid * 1e4 if mid else 0.0
            if abs(apx - px) <= 1e-9:
                rtype = "EXACT_REFILL"
            elif dist_bps <= nearby_max_bps:
                rtype = "NEARBY_REFILL"
            else:
                j += 1
                continue
            row = {
                "refill_type": rtype,
                "side": side,
                "original_price": px,
                "refill_price": apx,
                "removed_notional": removed,
                "refilled_notional": refilled,
                "refill_ratio": (refilled / removed) if removed else 0.0,
                "latency_ms": (at - rt).total_seconds() * 1000.0,
                "distance_bps": dist_bps,
                "confidence": "MEDIUM" if rtype == "EXACT_REFILL" else "LOW",
                "removal_event_id": rem.get("source_event_id"),
                "add_event_id": add.get("source_event_id"),
                "event_time": rt,
            }
            if best is None or row["latency_ms"] < best["latency_ms"]:
                best = row
            j += 1
        if best is None:
            out.append(
                {
                    "refill_type": "NO_REFILL_OBSERVED",
                    "side": side,
                    "original_price": px,
                    "refill_price": None,
                    "removed_notional": removed,
                    "refilled_notional": 0.0,
                    "refill_ratio": 0.0,
                    "latency_ms": None,
                    "distance_bps": None,
                    "confidence": "LOW",
                    "removal_event_id": rem.get("source_event_id"),
                    "add_event_id": None,
                    "event_time": rt,
                }
            )
        else:
            out.append(best)
    return out
