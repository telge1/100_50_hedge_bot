"""Adjacent union-interval gap detection (Signal_definierung + computeGaps semantics)."""

from __future__ import annotations

from typing import Any


def _band_lo_hi(b: dict[str, Any]) -> tuple[float, float]:
    return float(b["lower_price"]), float(b["upper_price"])


def union_intervals(bands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sorted_b = sorted(bands, key=lambda x: _band_lo_hi(x)[0])
    out: list[dict[str, Any]] = []
    for b in sorted_b:
        lo, hi = _band_lo_hi(b)
        if not out:
            out.append({"bottom_price": lo, "top_price": hi})
            continue
        last = out[-1]
        if lo <= last["top_price"] + 1e-15:
            last["top_price"] = max(last["top_price"], hi)
        else:
            out.append({"bottom_price": lo, "top_price": hi})
    return out


def compute_side_gaps(
    bands: list[dict[str, Any]],
    *,
    side: str,
    min_gap_pct: float,
) -> list[dict[str, Any]]:
    side_bands = [b for b in bands if str(b.get("side", "")).lower() == side]
    united = union_intervals(side_bands)
    gaps: list[dict[str, Any]] = []
    for i in range(len(united) - 1):
        prev = united[i]
        nxt = united[i + 1]
        gap_lo = prev["top_price"]
        gap_hi = nxt["bottom_price"]
        if gap_hi <= gap_lo + 1e-15:
            continue
        gap_pct = (gap_hi - gap_lo) / gap_lo * 100.0
        gaps.append(
            {
                "side": side,
                "gap_bottom_price": gap_lo,
                "gap_top_price": gap_hi,
                "gap_pct": gap_pct,
                "large_gap": gap_pct >= min_gap_pct,
            }
        )
    return gaps


def has_upper_large_gap_above_pool(
    upper_bands: list[dict[str, Any]],
    pool_upper: float,
    min_gap_pct: float,
) -> bool:
    """Mirror Phase 2A profile: large upper gap with gap_bottom >= pool upper edge."""
    gaps = compute_side_gaps(upper_bands, side="upper", min_gap_pct=min_gap_pct)
    for g in gaps:
        if not g["large_gap"]:
            continue
        if g["gap_bottom_price"] >= pool_upper - 1e-15:
            return True
    return False
