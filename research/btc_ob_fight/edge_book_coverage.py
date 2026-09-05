"""OB200 edge-region book coverage from in-memory replay snapshots."""

from __future__ import annotations

from typing import Any

from .profile_edge_state import price_to_tick

COVERAGE_FULL = "FULL_EDGE_REGION_COVERAGE"
COVERAGE_PARTIAL = "PARTIAL_EDGE_REGION_COVERAGE"
COVERAGE_OUTSIDE = "EDGE_REGION_OUTSIDE_BOOK_RANGE"
COVERAGE_MISSING = "BOOK_SAMPLE_MISSING"


def build_edge_book_coverage(
    ob_rows: list[dict[str, Any]],
    region_catalog: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Analyze edge-region visibility in reconstructed 200-level books."""
    coverage_rows: list[dict[str, Any]] = []
    depth_samples: list[dict[str, Any]] = []

    region_list: list[tuple[str, dict[str, Any], float, float]] = []
    for side_key in ("upper", "lower"):
        for reg in region_catalog.get(side_key) or []:
            lo, hi = reg.get("price_low"), reg.get("price_high")
            if lo is None or hi is None:
                continue
            region_list.append((side_key, reg, float(lo), float(hi)))

    for row in ob_rows:
        bid_map = ask_map = None
        if row.get("ok"):
            # Reuse precomputed maps when observability prepared the row.
            bid_map = row.get("_bid_map")
            ask_map = row.get("_ask_map")
            if bid_map is None:
                bid_map = _level_qty_by_tick(row.get("bids") or [])
            if ask_map is None:
                ask_map = _level_qty_by_tick(row.get("asks") or [])
        for _side_key, reg, lo, hi in region_list:
            sample = _sample_region(row, reg, lo, hi, bid_map=bid_map, ask_map=ask_map)
            coverage_rows.append(sample)
            if sample.get("coverage_status") in (COVERAGE_FULL, COVERAGE_PARTIAL):
                depth_samples.append(sample)

    summary = _summarize_coverage(coverage_rows, region_catalog)
    return coverage_rows, depth_samples, summary


def _sample_region(
    row: dict[str, Any],
    reg: dict[str, Any],
    lo: float,
    hi: float,
    *,
    bid_map: dict[int, float] | None = None,
    ask_map: dict[int, float] | None = None,
) -> dict[str, Any]:
    if not row.get("ok"):
        return {
            "sample_ts": row.get("ts"),
            "scope": reg.get("scope"),
            "edge": reg.get("edge"),
            "coverage_status": COVERAGE_MISSING,
            "region_price_low": lo,
            "region_price_high": hi,
        }

    bids = row.get("bids") or []
    asks = row.get("asks") or []
    bb = float(row.get("best_bid") or 0)
    ba = float(row.get("best_ask") or 0)
    mid = float(row.get("mid") or (bb + ba) / 2)
    spread = ba - bb

    lowest_bid = float(bids[-1][0]) if bids else bb
    highest_ask = float(asks[-1][0]) if asks else ba

    t0 = price_to_tick(lo)
    t1 = price_to_tick(hi - 1e-9)
    n_ticks = max(0, t1 - t0 + 1)
    if bid_map is None:
        bid_map = _level_qty_by_tick(bids)
    if ask_map is None:
        ask_map = _level_qty_by_tick(asks)

    # Iterate book levels (≤200/side) instead of every region tick — same counts.
    bid_qty = 0.0
    ask_qty = 0.0
    observed: set[int] = set()
    for tick, bq in bid_map.items():
        if t0 <= tick <= t1 and bq > 0:
            bid_qty += bq
            observed.add(tick)
    for tick, aq in ask_map.items():
        if t0 <= tick <= t1 and aq > 0:
            ask_qty += aq
            observed.add(tick)
    ticks_observed = len(observed)

    region_below_book = hi <= lowest_bid or lo >= highest_ask
    if region_below_book and not bids and not asks:
        status = COVERAGE_MISSING
    elif lo >= highest_ask or hi <= lowest_bid:
        if ticks_observed == 0:
            status = COVERAGE_OUTSIDE
        else:
            status = COVERAGE_PARTIAL
    elif ticks_observed >= n_ticks * 0.99 and n_ticks:
        status = COVERAGE_FULL
    elif ticks_observed > 0:
        status = COVERAGE_PARTIAL
    else:
        status = COVERAGE_OUTSIDE

    dist_bid = (bb - hi) if bb > hi else (lo - bb) if lo > bb else 0.0
    dist_ask = (lo - ba) if lo > ba else (ba - hi) if ba < hi else 0.0

    return {
        "sample_ts": row.get("ts"),
        "as_of": row.get("as_of"),
        "scope": reg.get("scope"),
        "edge": reg.get("edge"),
        "best_bid": bb,
        "best_ask": ba,
        "mid": mid,
        "spread": spread,
        "lowest_reconstructed_bid": lowest_bid,
        "highest_reconstructed_ask": highest_ask,
        "region_price_low": lo,
        "region_price_high": hi,
        "region_inside_book_span": not (lo >= highest_ask or hi <= lowest_bid),
        "ticks_in_region": n_ticks,
        "ticks_observed": ticks_observed,
        "visible_bid_qty_in_region": bid_qty,
        "visible_ask_qty_in_region": ask_qty,
        "distance_region_to_best_bid": dist_bid,
        "distance_region_to_best_ask": dist_ask,
        "coverage_status": status,
    }


def _ticks_in_range(lo: float, hi: float) -> list[int]:
    t0 = price_to_tick(lo)
    t1 = price_to_tick(hi - 1e-9)
    return list(range(t0, t1 + 1))


def _level_qty_by_tick(levels: list) -> dict[int, float]:
    out: dict[int, float] = {}
    for p, q in levels:
        out[price_to_tick(p)] = float(q)
    return out


def _qty_at_price(levels: list, price: float) -> float:
    target = price_to_tick(price)
    for p, q in levels:
        if price_to_tick(p) == target:
            return float(q)
    return 0.0


def _summarize_coverage(rows: list[dict[str, Any]], catalog: dict[str, Any]) -> dict[str, Any]:
    by_scope: dict[str, dict[str, int]] = {}
    for r in rows:
        scope = r.get("scope") or "UNKNOWN"
        by_scope.setdefault(scope, {})
        st = r.get("coverage_status") or "UNKNOWN"
        by_scope[scope][st] = by_scope[scope].get(st, 0) + 1
    return {
        "sample_count": len(rows),
        "by_scope_status": by_scope,
        "scopes_analyzed": list(by_scope.keys()),
        "note": "missing_coverage_does_not_imply_zero_consumption",
    }
