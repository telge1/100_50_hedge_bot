"""Wall persistence / consumption / migration proxies (causal only)."""

from __future__ import annotations

from typing import Any

import pandas as pd


def analyze_walls(
    *,
    level_changes: list[dict[str, Any]],
    bids_at_trigger: dict[float, float],
    asks_at_trigger: dict[float, float],
    mid_at_trigger: float,
    causal_end: pd.Timestamp,
    large_notional: float,
    migration_max_bps: float,
    migration_max_ms: int,
    partial_ratio: float,
    removed_ratio: float,
) -> list[dict[str, Any]]:
    causal_end = pd.to_datetime(causal_end, utc=True)
    walls: list[tuple[str, float, float]] = []
    for px, qty in bids_at_trigger.items():
        n = float(px) * float(qty)
        if n >= large_notional:
            walls.append(("bid", float(px), n))
    for px, qty in asks_at_trigger.items():
        n = float(px) * float(qty)
        if n >= large_notional:
            walls.append(("ask", float(px), n))

    changes = [e for e in level_changes if pd.to_datetime(e["event_time"], utc=True) < causal_end]
    out: list[dict[str, Any]] = []

    for side, px, notion0 in walls:
        removed = 0.0
        added_same = 0.0
        last_rem_ts = None
        for e in changes:
            if str(e["side"]) != side:
                continue
            if abs(float(e["price"]) - px) > 1e-9:
                continue
            sd = float(e["size_delta"])
            if sd < 0:
                removed += abs(float(e["notional_delta"]))
                last_rem_ts = pd.to_datetime(e["event_time"], utc=True)
            elif sd > 0:
                added_same += abs(float(e["notional_delta"]))

        rem_ratio = removed / notion0 if notion0 else 0.0
        if rem_ratio < 1e-6 and added_same < 1e-6:
            status = "WALL_PERSISTED"
        elif rem_ratio >= removed_ratio and added_same < removed * 0.2:
            status = "WALL_REMOVED_LIKELY"
        elif rem_ratio >= partial_ratio and rem_ratio < removed_ratio:
            status = "WALL_PARTIALLY_CONSUMED"
        elif added_same >= removed * 0.5 and removed > 0:
            status = "WALL_REFILLED"
        else:
            status = "WALL_INCONCLUSIVE"

        # migration proxy: removal then nearby add
        migrated = False
        if last_rem_ts is not None and rem_ratio >= partial_ratio:
            win = pd.Timedelta(milliseconds=migration_max_ms)
            for e in changes:
                at = pd.to_datetime(e["event_time"], utc=True)
                if at < last_rem_ts or at - last_rem_ts > win:
                    continue
                if str(e["side"]) != side:
                    continue
                if float(e["size_delta"]) <= 0:
                    continue
                apx = float(e["price"])
                dist = abs(apx - px) / mid_at_trigger * 1e4 if mid_at_trigger else 0.0
                if 1e-9 < dist <= migration_max_bps and abs(float(e["notional_delta"])) >= removed * 0.3:
                    migrated = True
                    status = "WALL_MIGRATED_PROXY"
                    break

        out.append(
            {
                "side": side,
                "price": px,
                "initial_notional": notion0,
                "removed_notional": removed,
                "refilled_same_level_notional": added_same,
                "wall_status": status,
                "migration_proxy": migrated,
                "proxy_note": "migration is proxy only; no trader identity",
            }
        )
    return out
