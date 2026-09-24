"""Helpers for restoring persisted burn plans after a restart."""

from __future__ import annotations

from typing import Any


def get_restorable_burn_plan(burn_state: Any) -> dict[str, float] | None:
    """Return persisted burn-plan fields when they are safe to restore."""
    if not isinstance(burn_state, dict):
        return None
    if not bool(burn_state.get("burn_planned", False)):
        return None

    stage = str(burn_state.get("stage", "") or "").strip().upper()
    if stage != "PLANNED":
        return None

    try:
        planned_burn_price = float(burn_state.get("planned_burn_price") or 0.0)
        planned_burn_size = float(burn_state.get("planned_burn_size") or 0.0)
    except (TypeError, ValueError):
        return None

    if planned_burn_price <= 0 or planned_burn_size <= 0:
        return None

    return {
        "planned_burn_price": planned_burn_price,
        "planned_burn_size": planned_burn_size,
    }
