"""Normalize band records for deterministic parity comparison."""

from __future__ import annotations

from typing import Any


def format_price(x: float | str) -> str:
    return format(float(x), ".17g")


def interval_to_band(
    iv: dict[str, Any],
    *,
    as_of_utc: str,
    pipeline_line_id: str | None,
    config_hash: str | None,
    pool_status: str = "ACTIVE",
) -> dict[str, Any]:
    side = str(iv.get("side", "")).lower()
    lo = iv["bottom_price"]
    hi = iv["top_price"]
    origins = list(iv.get("origins") or [])
    depth = int(iv.get("depth") or len(origins) or 0)
    return {
        "as_of_utc": as_of_utc,
        "side": side,
        "lower_price": format_price(lo),
        "upper_price": format_price(hi),
        "unique_origin_count": depth,
        "origin_ids": "|".join(sorted(str(o) for o in origins)),
        "pool_status": pool_status,
        "pipeline_line_id": pipeline_line_id,
        "config_hash": config_hash,
        "density_method": "exact_unique_origin",
        "active_semantics": "poolActiveAt(segment inclusive) @ floor5m(as_of)",
    }


def band_sort_key(b: dict[str, Any]) -> tuple:
    return (b["side"], float(b["lower_price"]), float(b["upper_price"]))
