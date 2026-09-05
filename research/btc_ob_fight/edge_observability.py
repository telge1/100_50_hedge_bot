"""Context-aware edge observability — fight-time coverage separate from full-window audit."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import BTCUSDT_TICK_SIZE
from .edge_book_coverage import (
    COVERAGE_FULL,
    COVERAGE_MISSING,
    COVERAGE_OUTSIDE,
    COVERAGE_PARTIAL,
    _level_qty_by_tick,
    _qty_at_price,
    _sample_region,
)
from .edge_regions import (
    SCOPE_EXACT_LEVEL_TICK,
    SCOPE_FIRST_OUTSIDE_BIN,
    SCOPE_PROFILE_EDGE_ZONE,
    SCOPE_TPO_EDGE_BIN,
    SCOPE_VOLUME_EDGE_BIN,
)
from .profile_edge_state import price_to_tick

EDGE_OBSERVABILITY_CONTRACT = "edge_observability_contract_v1"

TIME_FULL_WINDOW = "FULL_WINDOW_AUDIT"
TIME_EDGE_VISIT = "EDGE_VISIT_ACTIVE"
TIME_BETWEEN_ZONE = "BETWEEN_ZONE_ACTIVE"
TIME_OUTSIDE_EXCURSION = "OUTSIDE_EXCURSION_ACTIVE"
TIME_PRE_OUTSIDE = "PRE_OUTSIDE_WITHIN_VISIT"
TIME_POST_RECLAIM = "POST_RECLAIM_WITHIN_VISIT"

STATUS_OBSERVABLE = "OBSERVABLE"
STATUS_PARTIAL = "PARTIALLY_OBSERVABLE"
STATUS_NOT_OBSERVABLE = "NOT_OBSERVABLE"
STATUS_NO_SAMPLES = "NO_RELEVANT_SAMPLES"
STATUS_NOT_COMPUTED = "NOT_COMPUTED"

ALL_TIME_CONTEXTS = (
    TIME_FULL_WINDOW,
    TIME_EDGE_VISIT,
    TIME_BETWEEN_ZONE,
    TIME_OUTSIDE_EXCURSION,
    TIME_PRE_OUTSIDE,
    TIME_POST_RECLAIM,
)

PRIMARY_ATTACK_SIDE = {"UPPER": "ASK", "LOWER": "BID"}
CONTEXT_SIDE = {"UPPER": "BID", "LOWER": "ASK"}


def build_edge_observability(
    ob_rows: list[dict[str, Any]],
    region_catalog: dict[str, Any],
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
    reclaims: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    *,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build edge × time_context × scope observability rows."""
    time_spans = _build_time_context_spans(visits, excursions, reclaims, episodes, window_start, window_end)
    visit_count_by_edge = {"UPPER": 0, "LOWER": 0}
    for v in visits:
        e = v.get("edge")
        if e in visit_count_by_edge:
            visit_count_by_edge[e] += 1

    prepared = _prepare_ob_rows(ob_rows)
    detail_rows: list[dict[str, Any]] = []
    scopes = (
        SCOPE_EXACT_LEVEL_TICK,
        SCOPE_TPO_EDGE_BIN,
        SCOPE_VOLUME_EDGE_BIN,
        SCOPE_PROFILE_EDGE_ZONE,
        SCOPE_FIRST_OUTSIDE_BIN,
    )

    for edge in ("UPPER", "LOWER"):
        regions = [r for r in (region_catalog.get(edge.lower()) or region_catalog.get(edge) or []) if r.get("scope") in scopes]
        for time_ctx in ALL_TIME_CONTEXTS:
            if time_ctx != TIME_FULL_WINDOW and visit_count_by_edge[edge] == 0:
                for reg in regions:
                    detail_rows.append(_no_relevant_row(edge, time_ctx, reg))
                continue

            span_list = time_spans.get((edge, time_ctx), [])
            if time_ctx != TIME_FULL_WINDOW and not span_list:
                for reg in regions:
                    detail_rows.append(_no_relevant_row(edge, time_ctx, reg))
                continue

            filtered_ob = _filter_prepared_ob(prepared, span_list, time_ctx == TIME_FULL_WINDOW)
            for reg in regions:
                row = _aggregate_region_observability(filtered_ob, reg, edge, time_ctx, span_list, visits, excursions)
                detail_rows.append(row)

    summary = _summarize_observability(detail_rows, visit_count_by_edge)
    return detail_rows, summary


def _prepare_ob_rows(ob_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse timestamps once and prebuild tick maps for each snapshot."""
    prepared: list[dict[str, Any]] = []
    for row in ob_rows:
        ts_raw = row.get("ts") or row.get("as_of")
        ts_dt = None
        if ts_raw is not None:
            if isinstance(ts_raw, datetime):
                ts_dt = ts_raw
            else:
                ts_dt = _parse_ts(str(ts_raw))
        bid_map = ask_map = None
        if row.get("ok"):
            bid_map = row.get("_bid_map") or _level_qty_by_tick(row.get("bids") or [])
            ask_map = row.get("_ask_map") or _level_qty_by_tick(row.get("asks") or [])
            # Persist for downstream coverage reuse within same process.
            row["_bid_map"] = bid_map
            row["_ask_map"] = ask_map
        prepared.append({**row, "_ts_dt": ts_dt, "_bid_map": bid_map, "_ask_map": ask_map})
    return prepared


def _no_relevant_row(edge: str, time_ctx: str, reg: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge": edge,
        "time_context": time_ctx,
        "scope": reg.get("scope"),
        "status": STATUS_NO_SAMPLES,
        "primary_attack_side": PRIMARY_ATTACK_SIDE[edge],
        "context_side": CONTEXT_SIDE[edge],
        "sample_count": 0,
        "requested_tick_count": len(reg.get("ticks") or []),
        "observed_tick_count": 0,
        "tick_coverage_fraction": None,
        "full_coverage_count": 0,
        "full_coverage_pct": None,
        "partial_coverage_count": 0,
        "partial_coverage_pct": None,
        "outside_book_count": 0,
        "outside_book_pct": None,
        "missing_count": 0,
        "missing_pct": None,
        "edge_visit_count": 0,
        "outside_excursion_count": 0,
        "region_price_low": reg.get("price_low"),
        "region_price_high": reg.get("price_high"),
        "observed_price_low": None,
        "observed_price_high": None,
        "overlap_width": None,
        "locally_observed_region_fraction": None,
        "full_region_coverage": STATUS_NO_SAMPLES,
        "distance_region_to_best_bid": None,
        "distance_region_to_best_ask": None,
    }


def _build_time_context_spans(
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
    reclaims: list[dict[str, Any]],
    episodes: list[dict[str, Any]],
    window_start: datetime | None,
    window_end: datetime | None,
) -> dict[tuple[str, str], list[tuple[datetime, datetime]]]:
    spans: dict[tuple[str, str], list[tuple[datetime, datetime]]] = {}
    if window_start and window_end:
        spans[( "UPPER", TIME_FULL_WINDOW)] = [(window_start, window_end)]
        spans[( "LOWER", TIME_FULL_WINDOW)] = [(window_start, window_end)]

    reclaim_by_exc = {r["outside_excursion_id"]: r for r in reclaims if r.get("outside_excursion_id")}
    ep_by_id = {e["episode_id"]: e for e in episodes}

    for v in visits:
        edge = v.get("edge")
        if edge not in ("UPPER", "LOWER"):
            continue
        vs, ve = _parse_ts(v["start_ts"]), _parse_ts(v["end_ts"])
        spans.setdefault((edge, TIME_EDGE_VISIT), []).append((vs, ve))

        visit_eps = [ep_by_id[eid] for eid in (v.get("raw_episode_ids") or []) if eid in ep_by_id]
        between = [ep for ep in visit_eps if "BETWEEN" in ep.get("state", "")]
        for ep in between:
            spans.setdefault((edge, TIME_BETWEEN_ZONE), []).append(
                (_parse_ts(ep["start_ts"]), _parse_ts(ep["end_ts"]))
            )

        visit_excursions = [
            e for e in excursions
            if e.get("edge_visit_id") == v.get("edge_visit_id")
            or e.get("source_episode_id") in (v.get("raw_episode_ids") or [])
        ]
        first_outside_start = None
        for exc in sorted(visit_excursions, key=lambda x: x.get("start_ts", "")):
            es, ee = _parse_ts(exc["start_ts"]), _parse_ts(exc["end_ts"])
            spans.setdefault((edge, TIME_OUTSIDE_EXCURSION), []).append((es, ee))
            if first_outside_start is None:
                first_outside_start = es
            if exc.get("outside_excursion_id") in reclaim_by_exc:
                rcl = reclaim_by_exc[exc["outside_excursion_id"]]
                cross = _parse_ts(rcl["cross_ts"])
                spans.setdefault((edge, TIME_POST_RECLAIM), []).append((cross, ve))

        if first_outside_start:
            spans.setdefault((edge, TIME_PRE_OUTSIDE), []).append((vs, first_outside_start))

    return spans


def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _filter_ob_rows(
    ob_rows: list[dict[str, Any]],
    spans: list[tuple[datetime, datetime]],
    is_full_window: bool,
) -> list[dict[str, Any]]:
    prepared = _prepare_ob_rows(ob_rows)
    return _filter_prepared_ob(prepared, spans, is_full_window)


def _filter_prepared_ob(
    prepared: list[dict[str, Any]],
    spans: list[tuple[datetime, datetime]],
    is_full_window: bool,
) -> list[dict[str, Any]]:
    if is_full_window:
        return prepared
    if not spans:
        return []
    out = []
    for row in prepared:
        ts = row.get("_ts_dt")
        if ts is None:
            continue
        for start, end in spans:
            if start <= ts <= end:
                out.append(row)
                break
    return out


def _aggregate_region_observability(
    ob_rows: list[dict[str, Any]],
    reg: dict[str, Any],
    edge: str,
    time_ctx: str,
    spans: list[tuple[datetime, str]],
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
) -> dict[str, Any]:
    lo, hi = reg.get("price_low"), reg.get("price_high")
    if lo is None or hi is None:
        return {
            "edge": edge,
            "time_context": time_ctx,
            "scope": reg.get("scope"),
            "status": STATUS_NOT_COMPUTED,
            "reason": "REGION_BOUNDS_MISSING",
            "primary_attack_side": PRIMARY_ATTACK_SIDE[edge],
            "context_side": CONTEXT_SIDE[edge],
        }

    requested_ticks = reg.get("ticks") or list(range(price_to_tick(lo), price_to_tick(hi - 1e-9) + 1))
    if not requested_ticks and reg.get("scope") == SCOPE_EXACT_LEVEL_TICK:
        requested_ticks = [reg.get("price_tick")]

    if not ob_rows:
        return {
            **_no_relevant_row(edge, time_ctx, reg),
            "status": STATUS_NOT_OBSERVABLE if time_ctx != TIME_FULL_WINDOW else STATUS_NOT_OBSERVABLE,
            "sample_count": 0,
        }

    samples = []
    for row in ob_rows:
        samples.append(
            _sample_region(
                row,
                reg,
                lo,
                hi,
                bid_map=row.get("_bid_map"),
                ask_map=row.get("_ask_map"),
            )
        )
    n = len(samples)
    counts = {st: sum(1 for s in samples if s.get("coverage_status") == st) for st in (
        COVERAGE_FULL, COVERAGE_PARTIAL, COVERAGE_OUTSIDE, COVERAGE_MISSING
    )}

    def pct(c: int) -> float | None:
        return round(c / n * 100.0, 2) if n else None

    observed_lows = [s.get("lowest_reconstructed_bid") for s in samples if s.get("lowest_reconstructed_bid")]
    observed_highs = [s.get("highest_reconstructed_ask") for s in samples if s.get("highest_reconstructed_ask")]
    obs_lo = min(observed_lows) if observed_lows else None
    obs_hi = max(observed_highs) if observed_highs else None
    overlap_lo = max(lo, obs_lo) if obs_lo is not None else None
    overlap_hi = min(hi, obs_hi) if obs_hi is not None else None
    overlap_w = max(0.0, (overlap_hi - overlap_lo)) if overlap_lo is not None and overlap_hi is not None else None
    region_w = hi - lo
    local_frac = round(overlap_w / region_w, 4) if overlap_w is not None and region_w > 0 else None

    tick_obs = [s.get("ticks_observed") or 0 for s in samples]
    req = len(requested_ticks) or 1
    avg_tick_frac = sum((t / req) for t in tick_obs) / n if n else 0.0

    if counts[COVERAGE_FULL] / n >= 0.5 if n else False:
        status = STATUS_OBSERVABLE
        full_cov = COVERAGE_FULL
    elif (counts[COVERAGE_FULL] + counts[COVERAGE_PARTIAL]) / n >= 0.2 if n else False:
        status = STATUS_PARTIAL
        full_cov = COVERAGE_PARTIAL
    elif counts[COVERAGE_OUTSIDE] == n and n:
        status = STATUS_NOT_OBSERVABLE
        full_cov = COVERAGE_OUTSIDE
    elif counts[COVERAGE_MISSING] == n and n:
        status = STATUS_NOT_OBSERVABLE
        full_cov = COVERAGE_MISSING
    else:
        status = STATUS_PARTIAL
        full_cov = COVERAGE_PARTIAL

    edge_visits = sum(1 for v in visits if v.get("edge") == edge)
    edge_excursions = sum(1 for e in excursions if e.get("edge") == edge)

    dist_bid = sum(s.get("distance_region_to_best_bid") or 0 for s in samples) / n if n else None
    dist_ask = sum(s.get("distance_region_to_best_ask") or 0 for s in samples) / n if n else None

    return {
        "edge": edge,
        "time_context": time_ctx,
        "scope": reg.get("scope"),
        "status": status,
        "primary_attack_side": PRIMARY_ATTACK_SIDE[edge],
        "context_side": CONTEXT_SIDE[edge],
        "sample_count": n,
        "requested_tick_count": len(requested_ticks),
        "observed_tick_count": int(round(avg_tick_frac * len(requested_ticks))),
        "tick_coverage_fraction": round(avg_tick_frac, 4),
        "full_coverage_count": counts[COVERAGE_FULL],
        "full_coverage_pct": pct(counts[COVERAGE_FULL]),
        "partial_coverage_count": counts[COVERAGE_PARTIAL],
        "partial_coverage_pct": pct(counts[COVERAGE_PARTIAL]),
        "outside_book_count": counts[COVERAGE_OUTSIDE],
        "outside_book_pct": pct(counts[COVERAGE_OUTSIDE]),
        "missing_count": counts[COVERAGE_MISSING],
        "missing_pct": pct(counts[COVERAGE_MISSING]),
        "distance_region_to_best_bid": round(dist_bid, 2) if dist_bid is not None else None,
        "distance_region_to_best_ask": round(dist_ask, 2) if dist_ask is not None else None,
        "edge_visit_count": edge_visits,
        "outside_excursion_count": edge_excursions,
        "region_price_low": lo,
        "region_price_high": hi,
        "observed_price_low": obs_lo,
        "observed_price_high": obs_hi,
        "overlap_width": overlap_w,
        "locally_observed_region_fraction": local_frac,
        "full_region_coverage": full_cov,
    }


def _summarize_observability(rows: list[dict[str, Any]], visit_count_by_edge: dict[str, int]) -> dict[str, Any]:
    by_key: dict[str, list] = {}
    for r in rows:
        key = f"{r.get('edge')}|{r.get('time_context')}|{r.get('scope')}"
        by_key.setdefault(key, []).append(r)
    return {
        "contract_version": EDGE_OBSERVABILITY_CONTRACT,
        "row_count": len(rows),
        "visit_count_by_edge": visit_count_by_edge,
        "by_edge_time_scope": {k: v[0] if len(v) == 1 else v for k, v in by_key.items()},
    }
