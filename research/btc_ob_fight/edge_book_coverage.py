"""OB200 edge-region book coverage from in-memory replay snapshots."""

from __future__ import annotations

from typing import Any

from .config import BTCUSDT_TICK_SIZE
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

    for side_key in ("upper", "lower"):
        regions = region_catalog.get(side_key) or []
        for reg in regions:
            scope = reg.get("scope")
            lo, hi = reg.get("price_low"), reg.get("price_high")
            if lo is None or hi is None:
                continue
            for row in ob_rows:
                sample = _sample_region(row, reg, lo, hi)
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

    ticks_in_region = _ticks_in_range(lo, hi)
    bid_qty = 0.0
    ask_qty = 0.0
    ticks_observed = 0
    for tick in ticks_in_region:
        price = tick * BTCUSDT_TICK_SIZE
        bq = _qty_at_price(bids, price)
        aq = _qty_at_price(asks, price)
        if bq > 0 or aq > 0:
            ticks_observed += 1
        bid_qty += bq
        ask_qty += aq

    region_below_book = hi <= lowest_bid or lo >= highest_ask
    if region_below_book and not bids and not asks:
        status = COVERAGE_MISSING
    elif lo >= highest_ask or hi <= lowest_bid:
        if ticks_observed == 0:
            status = COVERAGE_OUTSIDE
        else:
            status = COVERAGE_PARTIAL
    elif ticks_observed >= len(ticks_in_region) * 0.99 and ticks_in_region:
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
        "ticks_in_region": len(ticks_in_region),
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


def _qty_at_price(levels: list, price: float) -> float:
    for p, q in levels:
        if abs(float(p) - price) < 1e-6:
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
