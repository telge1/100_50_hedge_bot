"""Map Phase-6C bridge ACTIVE members to legacy pool dicts for frozen short entry (read-only)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from research_charts.historical_lmv2_backtester_input_bridge_v1 import (
    HistoricalLmv2BacktesterInputStateV1,
)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_dt(value: str | datetime | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return _utc(dt)


def bridge_member_to_legacy(member: dict[str, Any], *, decision_close: datetime) -> dict[str, Any]:
    """One ACTIVE member → legacy dict for ``entry_setup`` / ``open_pools``."""
    dc = _utc(decision_close)
    known = _parse_dt(member.get("known_at"))
    if known is None:
        raise ValueError(f"pool {member.get('pool_id')} missing known_at")
    if known > dc:
        raise ValueError(f"pool {member.get('pool_id')} known_at after decision_close")

    lower = member.get("lower_bound")
    upper = member.get("upper_bound")
    if lower is None or upper is None:
        raise ValueError(f"pool {member.get('pool_id')} missing bounds")
    bottom = float(lower)
    top = float(upper)
    if not (top >= bottom > 0):
        raise ValueError(f"invalid bounds for {member.get('pool_id')}")

    break_at = _parse_dt(member.get("break_at"))
    source = _parse_dt(member.get("confirmed_at") or member.get("known_at"))

    return {
        "pool_id": str(member["pool_id"]),
        "side": str(member.get("side") or "").lower(),
        "bottom": bottom,
        "top": top,
        "known": known,
        "break_at": break_at,
        "source": source,
        "lmv2_snapshot_id": member.get("snapshot_id"),
        "lmv2_pipeline_line_id": member.get("pipeline_line_id"),
    }


def legacy_pools_from_bridge_state(
    state: HistoricalLmv2BacktesterInputStateV1,
    *,
    decision_close: datetime,
) -> list[dict[str, Any]]:
    """ACTIVE members from CH SSOT bridge only — no pool recompute."""
    dc = _utc(decision_close)
    close_iso = state.decision_close
    if close_iso and _parse_dt(close_iso) != dc:
        raise ValueError("bridge decision_close mismatch")
    out: list[dict[str, Any]] = []
    for member in state.active_members:
        out.append(bridge_member_to_legacy(dict(member), decision_close=dc))
    return out
