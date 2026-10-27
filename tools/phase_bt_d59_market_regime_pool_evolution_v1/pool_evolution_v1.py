"""Upper-pool structural evolution features (origin-aware, research-only)."""

from __future__ import annotations

from typing import Any

from setup_scanner_v1 import measure_upper_gap_pct  # type: ignore


def _upper_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for b in snapshot.get("upper_bands") or []:
        lo, hi = float(b["lower_price"]), float(b["upper_price"])
        rows.append(
            {
                "pool_lower": lo,
                "pool_upper": hi,
                "pool_key": f"{lo}|{hi}",
                "origin_ids": str(b.get("origin_ids") or ""),
                "origin_count": len([x for x in str(b.get("origin_ids") or "").split("|") if x.strip()]),
                "unique_origin_count": int(b.get("unique_origin_count") or 0),
            }
        )
    rows.sort(key=lambda r: (r["pool_lower"], r["pool_upper"]))
    return rows


def nearest_upper_above_close(uppers: list[dict[str, Any]], close: float) -> dict[str, Any] | None:
    for u in uppers:
        if u["pool_lower"] > close:
            return u
    return None


def next_separate_upper(uppers: list[dict[str, Any]], entry: dict[str, Any]) -> dict[str, Any] | None:
    for u in uppers:
        if u["pool_key"] == entry["pool_key"]:
            continue
        if u["pool_lower"] > entry["pool_upper"]:
            return u
    return None


def bands_between(uppers: list[dict[str, Any]], lo: float, hi: float) -> int:
    n = 0
    for u in uppers:
        if u["pool_lower"] >= lo and u["pool_upper"] <= hi:
            n += 1
        elif lo < u["pool_lower"] < hi:
            n += 1
    return max(0, n - 1)


def stack_density_near(uppers: list[dict[str, Any]], center_lo: float, center_hi: float, band_pct: float = 0.35) -> int:
    """Count upper bands whose mid lies within band_pct% of center mid (research metric)."""
    mid = (center_lo + center_hi) / 2.0
    tol = mid * (band_pct / 100.0)
    count = 0
    for u in uppers:
        um = (u["pool_lower"] + u["pool_upper"]) / 2.0
        if abs(um - mid) <= tol:
            count += 1
    return count


def compute_pool_evolution_row(
    *,
    decision_close_utc: str,
    snapshot: dict[str, Any],
    close: float,
    prior_entry: dict[str, Any] | None,
    min_gap_pct: float,
) -> dict[str, Any]:
    uppers = _upper_rows(snapshot)
    entry = nearest_upper_above_close(uppers, close)
    nxt = next_separate_upper(uppers, entry) if entry else None
    gap_pct = None
    if entry and nxt:
        gap_pct = measure_upper_gap_pct(uppers, entry["pool_upper"], min_gap_pct)
        if gap_pct is None and nxt["pool_lower"] > entry["pool_upper"]:
            gap_pct = (nxt["pool_lower"] - entry["pool_upper"]) / entry["pool_upper"] * 100.0

    continuity = "NO_PRIOR"
    bound_drift_lo = bound_drift_hi = None
    origin_added = origin_removed = False
    if entry and prior_entry:
        if entry["pool_key"] == prior_entry.get("pool_key"):
            continuity = "SAME_POOL_KEY"
        elif set(entry["origin_ids"].split("|")) & set(str(prior_entry.get("origin_ids") or "").split("|")):
            continuity = "ORIGIN_OVERLAP_DIFF_BOUNDS"
        else:
            continuity = "REPLACEMENT"
        bound_drift_lo = entry["pool_lower"] - float(prior_entry.get("pool_lower", 0))
        bound_drift_hi = entry["pool_upper"] - float(prior_entry.get("pool_upper", 0))
        old_o = set(x for x in str(prior_entry.get("origin_ids") or "").split("|") if x)
        new_o = set(x for x in entry["origin_ids"].split("|") if x)
        origin_added = bool(new_o - old_o)
        origin_removed = bool(old_o - new_o)

    row: dict[str, Any] = {
        "decision_close_utc": decision_close_utc,
        "close": close,
        "nearest_upper_pool_lower": entry["pool_lower"] if entry else None,
        "nearest_upper_pool_upper": entry["pool_upper"] if entry else None,
        "nearest_upper_pool_key": entry["pool_key"] if entry else None,
        "nearest_upper_origin_ids": entry["origin_ids"] if entry else None,
        "nearest_upper_origin_count": entry["origin_count"] if entry else None,
        "nearest_upper_width": (entry["pool_upper"] - entry["pool_lower"]) if entry else None,
        "distance_close_to_entry_lower_pct": (
            (entry["pool_lower"] - close) / close * 100.0 if entry and close else None
        ),
        "next_upper_pool_lower": nxt["pool_lower"] if nxt else None,
        "next_upper_pool_upper": nxt["pool_upper"] if nxt else None,
        "gap_to_next_upper_pct": gap_pct,
        "upper_bands_between_entry_and_next": (
            bands_between(uppers, entry["pool_upper"], nxt["pool_lower"]) if entry and nxt else None
        ),
        "local_stack_density_0p35pct": stack_density_near(uppers, entry["pool_lower"], entry["pool_upper"])
        if entry
        else None,
        "total_upper_band_count": len(uppers),
        "structural_continuity_vs_prior": continuity,
        "bound_drift_lower": bound_drift_lo,
        "bound_drift_upper": bound_drift_hi,
        "origin_ids_added": origin_added,
        "origin_ids_removed": origin_removed,
        "density_pipeline_line_id": snapshot.get("pipeline_line_id"),
        "density_config_hash": snapshot.get("config_hash"),
    }
    return row
