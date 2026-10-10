"""Map frozen historical V2 snapshot ACTIVE pools to chart zones (no engine recompute)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

HISTORICAL_LMV2_SNAPSHOT_LAYER = "historical_lmv2_decision_snapshot"
HISTORICAL_LMV2_SNAPSHOT_NAMESPACE = "HISTORICAL_LMV2_DECISION_SNAPSHOT"


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_dt(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).replace("Z", "+00:00")
    return _utc(datetime.fromisoformat(text))


def active_pool_records(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One dict per ACTIVE pool in snapshot order (active_pool_ids)."""
    ids = list(payload.get("active_pool_ids") or [])
    sides = list(payload.get("active_sides") or [])
    pools_map = payload.get("pools") or {}
    if len(ids) != len(sides):
        raise ValueError("active_pool_ids / active_sides length mismatch")
    if set(pools_map.keys()) != set(ids):
        raise ValueError("pools map keys != active_pool_ids")
    out: list[dict[str, Any]] = []
    for pool_id, side in zip(ids, sides):
        row = pools_map.get(pool_id)
        if row is None:
            raise ValueError(f"missing pool row for {pool_id}")
        if str(row.get("side", "")).lower() != str(side).lower():
            raise ValueError(f"side mismatch for {pool_id}")
        out.append(dict(row))
    return out


def snapshot_pools_to_chart_zones(
    payload: dict[str, Any],
    *,
    decision_close: datetime,
    symbol: str,
    timeframe: str,
    pool_detail: int,
) -> list[dict[str, Any]]:
    """
    One zone per ACTIVE pool; bounds copied exactly from snapshot JSON.

    ``end_timestamp`` extends one bar for visibility only; metadata marks decision snapshot.
    """
    from ..trp_import import ensure_trp_on_path

    ensure_trp_on_path()
    from indicators.live_micro_lld_v2.styles import BORDER_LOWER, BORDER_UPPER, FILL_BY_CLASS

    decision_close = _utc(decision_close)
    dc_unix = int(decision_close.timestamp())
    tf_sec = 900 if timeframe == "15m" else 300
    vis_end = dc_unix + tf_sec
    sym = str(symbol).upper()
    zones: list[dict[str, Any]] = []
    for row in active_pool_records(payload):
        pid = str(row["pool_id"])
        side = str(row["side"]).lower()
        bottom = float(row["bottom_price"])
        top = float(row["top_price"])
        sc = str(row.get("strength_class") or "")
        border = BORDER_UPPER if side == "upper" else BORDER_LOWER
        fill = FILL_BY_CLASS.get(sc, "#88888833")
        zones.append(
            {
                "id": f"historical-lmv2-snap:{pid}",
                "pool_id": pid,
                "namespace": HISTORICAL_LMV2_SNAPSHOT_NAMESPACE,
                "type": "zone",
                "side": side,
                "pool_type": row.get("pool_type") or "micro",
                "strength_class": sc,
                "lower_price": bottom,
                "upper_price": top,
                "bottom_price": bottom,
                "top_price": top,
                "start_timestamp": dc_unix,
                "end_timestamp": vis_end,
                "style": {"color": border, "fillColor": fill, "lineWidth": 2},
                "metadata": {
                    "source": HISTORICAL_LMV2_SNAPSHOT_LAYER,
                    "display_mode": "decision_snapshot_pin",
                    "decision_close": decision_close.isoformat(),
                    "symbol": sym,
                    "timeframe": timeframe,
                    "pool_detail": int(pool_detail),
                    "strength": row.get("strength"),
                    "known_at": row.get("known_at"),
                    "row_fingerprint": row.get("row_fingerprint"),
                },
            }
        )
    return zones


def zone_geometry_by_pool_id(zones: list[dict[str, Any]]) -> dict[str, tuple[float, float]]:
    out: dict[str, tuple[float, float]] = {}
    for z in zones:
        pid = str(z["pool_id"])
        if pid in out:
            raise ValueError(f"duplicate zone pool_id {pid}")
        out[pid] = (float(z["bottom_price"]), float(z["top_price"]))
    return out
