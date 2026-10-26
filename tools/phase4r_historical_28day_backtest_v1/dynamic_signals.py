"""Attach per-bar dynamic pool geometry to signal feed (no multi-day static profiles)."""

from __future__ import annotations

import copy
from typing import Any

from snapshot_index import intervals_from_snapshot, upper_tp_rows_from_snapshot


def apply_dynamic_snapshots(signals: list[Any], snap_index: dict[str, dict]) -> tuple[list[Any], list[dict]]:
    """Return new signal list and unresolved rows for missing snapshot bars."""
    out = []
    unresolved = []
    for sig in signals:
        s = copy.copy(sig)
        snap = snap_index.get(sig.signal_close_utc)
        if not snap:
            unresolved.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": "DYNAMIC_SNAPSHOT_MISSING_AT_SIGNAL_CLOSE",
                }
            )
            out.append(s)
            continue
        s.landscape_intervals = intervals_from_snapshot(snap)
        if sig.direction == "LONG":
            s.upper_tp_rows = upper_tp_rows_from_snapshot(snap)
        pool_l = float(sig.pool_lower)
        pool_u = float(sig.pool_upper)
        matched = False
        side = "upper" if sig.direction == "SHORT" else "lower"
        for p in snap.get(f"{side}_pools") or []:
            if abs(float(p["bottom_price"]) - pool_l) < 1e-8 and abs(float(p["top_price"]) - pool_u) < 1e-8:
                matched = True
                break
        if not matched:
            unresolved.append(
                {
                    "candidate_id": sig.candidate_id,
                    "signal_close_utc": sig.signal_close_utc,
                    "reason": "POOL_IDENTITY_UNRESOLVED_AT_SIGNAL_CLOSE",
                    "detail": "Static candidate bounds not found in causal depth-2 set at close",
                }
            )
        out.append(s)
    return out, unresolved
