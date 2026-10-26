"""Convert BT-D4 density snapshots to interval dicts for TP selection."""

from __future__ import annotations

from typing import Any


def snapshot_to_intervals(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for side_key, side_name in (("upper_bands", "upper"), ("lower_bands", "lower")):
        for b in snapshot.get(side_key) or []:
            depth = int(b.get("unique_origin_count") or 2)
            out.append(
                {
                    "side": side_name,
                    "bottom_price": float(b["lower_price"]),
                    "top_price": float(b["upper_price"]),
                    "depth": depth,
                    "origin_ids": b.get("origin_ids"),
                }
            )
    return out
