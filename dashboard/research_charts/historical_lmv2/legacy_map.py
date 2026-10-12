"""Map original V2 snapshot pools to the legacy short-strategy pool dict contract."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def v2_pool_to_legacy(row: dict[str, Any], *, decision_close: datetime) -> dict[str, Any]:
    """Build one pool dict for ``entry_setup`` / ``open_pools`` / guard helpers."""
    known = _parse_dt(row.get("known_at"))
    if known is None:
        raise ValueError(f"pool {row.get('pool_id')} missing known_at")
    if known > decision_close:
        raise ValueError(f"pool {row.get('pool_id')} known_at after decision_close")

    break_at = _parse_dt(row.get("break_at"))
    source = _parse_dt(row.get("pivot_timestamp") or row.get("candidate_created_at"))

    return {
        "pool_id": str(row["pool_id"]),
        "side": str(row["side"]).lower(),
        "bottom": float(row["bottom_price"]),
        "top": float(row["top_price"]),
        "known": known,
        "break_at": break_at,
        "source": source,
    }


def legacy_pools_from_snapshot_payload(
    payload: dict[str, Any],
    *,
    decision_close: datetime,
) -> list[dict[str, Any]]:
    """ACTIVE membership only — no chart selection."""
    decision_close = decision_close if decision_close.tzinfo else decision_close.replace(tzinfo=timezone.utc)
    ids = list(payload.get("active_pool_ids") or [])
    sides = list(payload.get("active_sides") or [])
    pools_map = payload.get("pools") or {}
    if set(pools_map.keys()) != set(ids) or len(ids) != len(sides):
        raise ValueError("active_pool_ids / active_sides / pools map mismatch")

    out: list[dict[str, Any]] = []
    for pool_id, side in zip(ids, sides):
        row = pools_map.get(pool_id)
        if row is None:
            raise ValueError(f"missing pool row for {pool_id}")
        if str(row.get("side", "")).lower() != str(side).lower():
            raise ValueError(f"side mismatch for {pool_id}")
        top = float(row["top_price"])
        bottom = float(row["bottom_price"])
        if not (top >= bottom > 0):
            raise ValueError(f"invalid bounds for {pool_id}")
        legacy = v2_pool_to_legacy(row, decision_close=decision_close)
        out.append(legacy)
    return out
