"""Weak intermediate pool tagging (research only — V1 filter disabled)."""

from __future__ import annotations

from typing import Any

WEAK_MIN_WIDTH_PCT = 0.10
WEAK_MIN_ACTIVE_BARS = 5


def tag_weak_intermediate_pools(
    snapshot: dict[str, Any],
    *,
    consecutive_active_bars: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Return audit tags; does not remove pools from selection in V1."""
    out: list[dict[str, Any]] = []
    for b in snapshot.get("lower_bands") or []:
        if str(b.get("pool_status") or "").upper() != "ACTIVE":
            continue
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        mid = (lo + hi) / 2.0 if (lo + hi) else lo
        width_pct = (hi - lo) / mid * 100.0 if mid else 0.0
        pk = f"{b['lower_price']}|{b['upper_price']}"
        active_bars = (consecutive_active_bars or {}).get(pk, 0)
        weak = width_pct + 1e-12 < WEAK_MIN_WIDTH_PCT or active_bars < WEAK_MIN_ACTIVE_BARS
        out.append(
            {
                "pool_key": pk,
                "width_pct": round(width_pct, 8),
                "consecutive_active_bars": active_bars,
                "weak_intermediate_tag": weak,
                "filter_would_apply_in_variant_b": weak,
            }
        )
    return out
