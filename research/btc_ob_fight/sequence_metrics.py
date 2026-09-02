"""Enhanced sequence metrics for reporting (coverage, consumption, refills, OI)."""

from __future__ import annotations

from typing import Any

COVERAGE_FULL = "FULL_EDGE_REGION_COVERAGE"
COVERAGE_PARTIAL = "PARTIAL_EDGE_REGION_COVERAGE"
COVERAGE_OUTSIDE = "EDGE_REGION_OUTSIDE_BOOK_RANGE"
COVERAGE_MISSING = "BOOK_SAMPLE_MISSING"


def build_ob_coverage_metrics(coverage_rows: list[dict[str, Any]]) -> dict[str, Any]:
    scopes = (
        "EXACT_LEVEL_TICK",
        "TPO_EDGE_BIN",
        "VOLUME_EDGE_BIN",
        "PROFILE_EDGE_ZONE",
        "FIRST_OUTSIDE_BIN",
    )
    by_scope: dict[str, dict[str, Any]] = {}
    total = len(coverage_rows)
    for sc in scopes:
        rows = [r for r in coverage_rows if r.get("scope") == sc]
        n = len(rows)
        if n == 0:
            by_scope[sc] = {"sample_count": 0}
            continue
        counts = {st: sum(1 for r in rows if r.get("coverage_status") == st) for st in (
            COVERAGE_FULL, COVERAGE_PARTIAL, COVERAGE_OUTSIDE, COVERAGE_MISSING
        )}

        def pct(c: int) -> float:
            return round(c / n * 100.0, 2) if n else 0.0

        by_scope[sc] = {
            "sample_count": n,
            "full_count": counts[COVERAGE_FULL],
            "full_pct": pct(counts[COVERAGE_FULL]),
            "partial_count": counts[COVERAGE_PARTIAL],
            "partial_pct": pct(counts[COVERAGE_PARTIAL]),
            "outside_book_range_count": counts[COVERAGE_OUTSIDE],
            "outside_book_range_pct": pct(counts[COVERAGE_OUTSIDE]),
            "missing_sample_count": counts[COVERAGE_MISSING],
            "missing_sample_pct": pct(counts[COVERAGE_MISSING]),
        }

    scoped_rows = [r for r in coverage_rows if r.get("scope") in scopes]
    n_all = len(scoped_rows)
    if n_all:
        all_counts = {st: sum(1 for r in scoped_rows if r.get("coverage_status") == st) for st in (
            COVERAGE_FULL, COVERAGE_PARTIAL, COVERAGE_OUTSIDE, COVERAGE_MISSING
        )}

        def pct_all(c: int) -> float:
            return round(c / n_all * 100.0, 2)

        overall = {
            "sample_count": n_all,
            "full_count": all_counts[COVERAGE_FULL],
            "full_coverage_pct": pct_all(all_counts[COVERAGE_FULL]),
            "partial_count": all_counts[COVERAGE_PARTIAL],
            "partial_coverage_pct": pct_all(all_counts[COVERAGE_PARTIAL]),
            "outside_book_range_count": all_counts[COVERAGE_OUTSIDE],
            "outside_book_range_pct": pct_all(all_counts[COVERAGE_OUTSIDE]),
            "missing_sample_count": all_counts[COVERAGE_MISSING],
            "missing_sample_pct": pct_all(all_counts[COVERAGE_MISSING]),
        }
    else:
        overall = {"sample_count": 0}

    return {"by_scope": by_scope, "total_sample_rows": total, "overall": overall}


def build_consumption_metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    by_scope_side: dict[str, dict[str, Any]] = {}
    for e in events:
        sc = e.get("scope") or "UNKNOWN"
        side = e.get("side") or "UNKNOWN"
        key = f"{sc}|{side}"
        slot = by_scope_side.setdefault(
            key,
            {
                "scope": sc,
                "side": side,
                "event_count": 0,
                "trade_associated_count": 0,
                "unmatched_count": 0,
                "disappearance_count": 0,
                "matched_trade_volume": 0.0,
                "visible_qty_reduction": 0.0,
                "affected_ticks": set(),
                "edge_visit_ids": set(),
            },
        )
        slot["event_count"] += 1
        if e.get("matching_status") == "TRADE_ASSOCIATED":
            slot["trade_associated_count"] += 1
        else:
            slot["unmatched_count"] += 1
        et = str(e.get("event_type") or "")
        if "DISAPPEARANCE" in et:
            slot["disappearance_count"] += 1
        slot["matched_trade_volume"] += float(e.get("matched_trade_volume") or 0)
        slot["visible_qty_reduction"] += float(e.get("visible_qty_reduction") or 0)
        if e.get("price_tick") is not None:
            slot["affected_ticks"].add(int(e["price_tick"]))
        if e.get("edge_visit_id"):
            slot["edge_visit_ids"].add(e["edge_visit_id"])

    rows = []
    for slot in by_scope_side.values():
        rows.append(
            {
                **{k: v for k, v in slot.items() if k not in ("affected_ticks", "edge_visit_ids")},
                "affected_tick_count": len(slot["affected_ticks"]),
                "edge_visit_count": len(slot["edge_visit_ids"]),
            }
        )
    return {"rows": rows, "total_events": len(events)}


def build_coverage_aware_consumption_metrics(
    events: list[dict[str, Any]],
    visits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Consumption metrics with coverage status and canonical eligibility."""
    visit_ids = {v["edge_visit_id"] for v in visits}
    rows: list[dict[str, Any]] = []
    by_key: dict[str, dict[str, Any]] = {}

    for e in events:
        edge = e.get("edge") or "UNKNOWN"
        scope = e.get("scope") or "UNKNOWN"
        side = e.get("side") or "UNKNOWN"
        cov = e.get("coverage_status") or "UNKNOWN"
        in_visit = e.get("edge_visit_id") in visit_ids if e.get("edge_visit_id") else False
        time_ctx = "EDGE_VISIT_ACTIVE" if in_visit else "FULL_WINDOW_AUDIT"
        key = f"{edge}|{time_ctx}|{scope}|{side}|{cov}"
        slot = by_key.setdefault(
            key,
            {
                "edge": edge,
                "time_context": time_ctx,
                "scope": scope,
                "side": side,
                "coverage_status": cov,
                "canonical_eligible": in_visit and cov in ("FULL_EDGE_REGION_COVERAGE", "PARTIAL_EDGE_REGION_COVERAGE"),
                "event_count": 0,
                "trade_associated_count": 0,
                "unmatched_count": 0,
                "disappearance_count": 0,
                "matched_trade_volume": 0.0,
                "visible_qty_reduction": 0.0,
                "affected_ticks": set(),
                "edge_visit_ids": set(),
                "outside_excursion_ids": set(),
            },
        )
        slot["event_count"] += 1
        if e.get("matching_status") == "TRADE_ASSOCIATED":
            slot["trade_associated_count"] += 1
        else:
            slot["unmatched_count"] += 1
        if "DISAPPEARANCE" in str(e.get("event_type") or ""):
            slot["disappearance_count"] += 1
        slot["matched_trade_volume"] += float(e.get("matched_trade_volume") or 0)
        slot["visible_qty_reduction"] += float(e.get("visible_qty_reduction") or 0)
        if e.get("price_tick") is not None:
            slot["affected_ticks"].add(int(e["price_tick"]))
        if e.get("edge_visit_id"):
            slot["edge_visit_ids"].add(e["edge_visit_id"])
        if e.get("outside_excursion_id"):
            slot["outside_excursion_ids"].add(e["outside_excursion_id"])

    for slot in by_key.values():
        obs_ok = slot["coverage_status"] in ("FULL_EDGE_REGION_COVERAGE", "PARTIAL_EDGE_REGION_COVERAGE")
        if slot["event_count"] == 0 and obs_ok:
            event_status = "NO_EVENT_OBSERVED"
        elif not obs_ok:
            event_status = "EVENT_STATUS_UNKNOWN_DUE_TO_COVERAGE"
        else:
            event_status = "EVENTS_OBSERVED"
        rows.append(
            {
                **{k: v for k, v in slot.items() if not isinstance(v, set)},
                "affected_tick_count": len(slot["affected_ticks"]),
                "edge_visit_count": len(slot["edge_visit_ids"]),
                "outside_excursion_count": len(slot["outside_excursion_ids"]),
                "event_status": event_status,
            }
        )
    return {"rows": rows, "total_events": len(events)}


def build_nearby_liquidity_metrics(nearby: list[dict[str, Any]]) -> dict[str, Any]:
    ask = sum(1 for r in nearby if r.get("side") == "ASK")
    bid = sum(1 for r in nearby if r.get("side") == "BID")
    unknown = sum(1 for r in nearby if r.get("side") not in ("ASK", "BID"))
    return {
        "total_count": len(nearby),
        "ask_count": ask,
        "bid_count": bid,
        "unknown_count": unknown,
        "ask_plus_bid_plus_unknown_equals_total": ask + bid + unknown == len(nearby),
    }


def build_refill_metrics(
    exact_refills: list[dict[str, Any]],
    nearby: list[dict[str, Any]],
) -> dict[str, Any]:
    ask_exact = [r for r in exact_refills if r.get("side") == "ASK"]
    bid_exact = [r for r in exact_refills if r.get("side") == "BID"]
    partial = sum(1 for r in exact_refills if "PARTIAL" in str(r.get("event_type", "")))
    full = sum(1 for r in exact_refills if "FULL" in str(r.get("event_type", "")))
    delays = [float(r["seconds_after_consumption"]) for r in exact_refills if r.get("seconds_after_consumption") is not None]
    tick_dists = [int(r["tick_distance"]) for r in nearby if r.get("tick_distance") is not None]
    return {
        "exact_same_tick_refills": len(exact_refills),
        "exact_ask_count": len(ask_exact),
        "exact_bid_count": len(bid_exact),
        "partial_recovery_count": partial,
        "full_recovery_count": full,
        "nearby_liquidity_increase_count": len(nearby),
        "nearby_ask_count": sum(1 for r in nearby if r.get("side") == "ASK"),
        "nearby_bid_count": sum(1 for r in nearby if r.get("side") == "BID"),
        "nearby_unknown_count": sum(1 for r in nearby if r.get("side") not in ("ASK", "BID")),
        "refill_delay_seconds_min": min(delays) if delays else None,
        "refill_delay_seconds_median": sorted(delays)[len(delays) // 2] if delays else None,
        "refill_delay_seconds_max": max(delays) if delays else None,
        "nearby_tick_distance_min": min(tick_dists) if tick_dists else None,
        "nearby_tick_distance_max": max(tick_dists) if tick_dists else None,
    }


def _duration_stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0}
    s = sorted(values)

    def pct(p: float) -> float:
        idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return s[idx]

    return {
        "count": len(values),
        "min": s[0],
        "p25": pct(0.25),
        "median": pct(0.5),
        "p75": pct(0.75),
        "p90": pct(0.9),
        "p95": pct(0.95),
        "max": s[-1],
        "total": sum(values),
    }


def build_outside_excursion_category_metrics(
    raw: list[dict[str, Any]],
    canonical: list[dict[str, Any]],
    ambiguous: list[dict[str, Any]],
) -> dict[str, Any]:
    def pack(eps: list[dict[str, Any]]) -> dict[str, Any]:
        durs = [float(e.get("duration_seconds") or 0) for e in eps]
        reclaim_ts = {e.get("reclaim_event_id") for e in eps if e.get("reclaim_event_id")}
        return {
            "count": len(eps),
            "unique_start_timestamps": len({e.get("start_ts") for e in eps}),
            "unique_reclaim_timestamps": len(reclaim_ts),
            "zero_duration_count": sum(1 for d in durs if d == 0),
            "duration": _duration_stats(durs),
            "base_volume": sum(float(e.get("base_volume") or 0) for e in eps),
            "quote_volume": sum(float(e.get("quote_notional") or 0) for e in eps),
            "taker_buy_quote": sum(float(e.get("taker_buy_quote") or 0) for e in eps),
            "taker_sell_quote": sum(float(e.get("taker_sell_quote") or 0) for e in eps),
            "taker_delta_quote": sum(float(e.get("taker_delta_quote") or 0) for e in eps),
            "reclaim_count": sum(1 for e in eps if e.get("reclaimed")),
            "open_count": sum(1 for e in eps if not e.get("reclaimed")),
        }

    return {
        "raw_outside_excursions": pack(raw),
        "canonical_outside_excursions": pack(canonical),
        "ambiguous_same_timestamp_excursions": pack(ambiguous),
    }
