"""
Canonical Exact Unique-Origin D2 bands — Python port of
`static/js/research/pd1_exact_density_trp_adapter.js` (`computeExactBandsForSide`).

Behaviour is intended to match the Research Chart overlay at a single decision instant:
ACTIVE pools only, origin via `historical_window_v1._origin_logical_v2_key`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

D2_COMPUTE_VERSION = "pd1_exact_density_trp_adapter.computeExactBandsForSide-parity-v1"
D2_MIN_DEPTH_DEFAULT = 2


@dataclass(frozen=True)
class D2Band:
    bottom: float
    top: float
    depth: int


def origin_logical_v2_key(pool_id: str) -> str:
    """Same as `historical_window_v1._origin_logical_v2_key` (local copy for isolated L1 imports)."""
    parts = str(pool_id).split(":")
    if len(parts) >= 6 and parts[0] == "lldv2":
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[4]}:{parts[5]}"
    if len(parts) >= 7:
        return f"{parts[0]}:{parts[1]}:{parts[2]}:{parts[5]}:{parts[6]}"
    return str(pool_id)


def _iso_ms(dc: datetime) -> int:
    if dc.tzinfo is None:
        dc = dc.replace(tzinfo=timezone.utc)
    return int(dc.timestamp() * 1000)


def members_to_instant_pools(members: list[dict[str, Any]], decision_close: datetime) -> list[dict[str, Any]]:
    """At one published decision_close every catalog member is ACTIVE for that instant."""
    t_ms = _iso_ms(decision_close)
    seg = [t_ms, t_ms]
    out: list[dict[str, Any]] = []
    for m in members:
        pid = str(m["pool_id"])
        out.append(
            {
                "pool_id": pid,
                "side": str(m.get("side") or ""),
                "bottom": float(m["lower_bound"]),
                "top": float(m["upper_bound"]),
                "origin": origin_logical_v2_key(pid),
                "segments": [seg],
            }
        )
    return out


def _pool_active_at(pl: dict[str, Any], t_ms: int) -> bool:
    for seg in pl.get("segments") or []:
        if len(seg) < 2:
            continue
        a, b = int(seg[0]), int(seg[1])
        if t_ms >= a and t_ms <= b:
            return True
    return False


def compute_exact_bands_for_side(
    pools: list[dict[str, Any]],
    t_ms: int,
    side: str,
    y_min: float,
    y_max: float,
) -> list[D2Band]:
    """Port of computeExactBandsForSide (JS lines 40–73)."""
    active = [pl for pl in pools if _pool_active_at(pl, t_ms) and pl.get("side") == side]
    if not active:
        return []
    events: list[tuple[float, int, str, str]] = []
    for pl in active:
        lo = max(y_min, float(pl["bottom"]))
        hi = min(y_max, float(pl["top"]))
        if hi < lo:
            continue
        events.append((lo, 1, str(pl["pool_id"]), str(pl["origin"])))
        events.append((hi, -1, str(pl["pool_id"]), str(pl["origin"])))
    if not events:
        return []
    events.sort(key=lambda e: (e[0], -e[1]))
    active_origins: dict[str, str] = {}
    bands_raw: list[tuple[float, float, int]] = []
    prev_price: float | None = None
    prev_depth = 0

    def depth_now() -> int:
        return len(set(active_origins.values()))

    for price, delta, pid, origin in events:
        if prev_price is not None and price > prev_price and prev_depth > 0:
            bands_raw.append((prev_price, price, prev_depth))
        if delta > 0:
            active_origins[pid] = origin
        else:
            active_origins.pop(pid, None)
        prev_depth = depth_now()
        prev_price = price

    merged: list[D2Band] = []
    for y0, y1, depth in bands_raw:
        if (
            merged
            and merged[-1].depth == depth
            and abs(y0 - merged[-1].top) < 1e-15
        ):
            merged[-1] = D2Band(merged[-1].bottom, y1, depth)
        else:
            merged.append(D2Band(y0, y1, depth))
    return merged


def price_window_for_entry(entry: float) -> tuple[float, float]:
    """Research L1 audit window (matches L1 V3A parity script)."""
    return entry * 0.92, entry * 1.15


def upper_d2_bands_at_close(
    members: list[dict] | None,
    decision_close: datetime,
    entry: float,
    *,
    min_depth: int = D2_MIN_DEPTH_DEFAULT,
) -> tuple[list[D2Band], str | None]:
    if members is None:
        return [], "UNRESOLVED_POOL_SNAPSHOT"
    pools = members_to_instant_pools(members, decision_close)
    y_min, y_max = price_window_for_entry(entry)
    t_ms = _iso_ms(decision_close)
    all_bands = compute_exact_bands_for_side(pools, t_ms, "upper", y_min, y_max)
    d2 = [b for b in all_bands if b.depth >= min_depth]
    d2.sort(key=lambda b: b.bottom)
    return d2, None


def qualify_upper_d2_bands(
    bands: list[D2Band],
    entry: float,
    min_dist_pct: float,
) -> list[dict[str, Any]]:
    """Gate rows compatible with run_l1_pilot.pick_tp (D2 band bottom = target)."""
    out: list[dict[str, Any]] = []
    for b in bands:
        if b.bottom <= entry:
            continue
        dist = (b.bottom - entry) / entry * 100.0
        if dist < min_dist_pct - 1e-12:
            continue
        out.append(
            {
                "lower_bound": b.bottom,
                "upper_bound": b.top,
                "pool_id": f"d2-exact:{b.bottom:.10f}:{b.top:.10f}:d{b.depth}",
                "side": "upper",
                "d2_depth": b.depth,
                "width_pct": (b.top - b.bottom) / b.bottom * 100.0 if b.bottom > 0 else 0.0,
            }
        )
    return out
