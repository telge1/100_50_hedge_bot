"""As-of pool inventory and deterministic next-pool selection (causal)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.liquidity_pool_signal import chart_lookback_start
from orderbook_analyse.liquidity_pool_signal.chart_pool_adapter import (
    pool_row_from_engine,
    run_chart_backend_lld,
)
from orderbook_analyse.post_case_02_next_pool_causal_reaction_audit_v1 import (
    OUTCOME_USED_FOR_MATCHING,
    OUTCOME_USED_FOR_POOL_SELECTION,
    OUTCOME_USED_FOR_STATE_DEFINITION,
    OUTCOME_USED_FOR_THRESHOLDS,
    REF_TS,
    SYMBOL,
    TF_DURATION_S,
    TIMEFRAMES,
)


def _utc(ts: str | datetime) -> datetime:
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return ts.replace(tzinfo=timezone.utc)
        return ts.astimezone(timezone.utc)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return _utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def bps(a: float, b: float) -> float:
    if b <= 0:
        return float("nan")
    return (a - b) / b * 10000.0


def intervals_overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    return a_lo <= b_hi and b_lo <= a_hi


def build_asof_inventory(
    *,
    as_of: datetime,
    market_price: float,
) -> list[dict[str, Any]]:
    """All confirmed active pools as-of; one engine pack per TF ending at as_of."""
    rows: list[dict[str, Any]] = []
    as_of_u = _utc(as_of)
    for tf in TIMEFRAMES:
        start = chart_lookback_start(as_of_u, tf)
        bundle = run_chart_backend_lld(
            symbol=SYMBOL, timeframe=tf, start=start, end=as_of_u
        )
        for p in bundle["engine_result"].pools:
            r = pool_row_from_engine(
                p, cfg=bundle["config"], as_of=as_of_u, market_price=market_price
            )
            # Causal: available_at <= as_of already encoded in active_as_of
            if not r["active_as_of"]:
                continue
            width_bps = bps(r["upper_edge"], r["lower_edge"])
            front = r["lower_edge"] if r["side"] == "ASK" else r["upper_edge"]
            dist_front = None
            if r["side"] == "ASK":
                dist_front = bps(front, market_price)  # positive => above
            else:
                dist_front = bps(market_price, front)
            rows.append(
                {
                    **r,
                    "front_edge": front,
                    "back_edge": r["upper_edge"] if r["side"] == "ASK" else r["lower_edge"],
                    "width_bps": width_bps,
                    "distance_to_front_edge_bps": dist_front,
                    "tf_duration_s": TF_DURATION_S[tf],
                    "market_price_asof": market_price,
                    "as_of": _iso(as_of_u),
                }
            )
    # Overlap annotations across TFs (ASK only for this audit inventory view)
    ask = [r for r in rows if r["side"] == "ASK"]
    for r in rows:
        overlaps = []
        if r["side"] == "ASK":
            for o in ask:
                if o["pool_id"] == r["pool_id"] and o["source_timeframe"] == r["source_timeframe"]:
                    continue
                if intervals_overlap(
                    r["lower_edge"], r["upper_edge"], o["lower_edge"], o["upper_edge"]
                ):
                    overlaps.append(
                        {
                            "pool_id": o["pool_id"],
                            "source_timeframe": o["source_timeframe"],
                            "lower_edge": o["lower_edge"],
                            "upper_edge": o["upper_edge"],
                        }
                    )
        r["overlapping_other_tf_pools"] = overlaps
        r["forms_shared_price_component"] = len(overlaps) > 0
    return rows


def ask_entirely_above(rows: list[dict[str, Any]], market: float) -> list[dict[str, Any]]:
    return [
        r
        for r in rows
        if r["side"] == "ASK" and float(r["lower_edge"]) > market
    ]


def ask_containing(rows: list[dict[str, Any]], market: float) -> list[dict[str, Any]]:
    return [
        r
        for r in rows
        if r["side"] == "ASK"
        and float(r["lower_edge"]) <= market <= float(r["upper_edge"])
    ]


def ask_remaining_ceiling(rows: list[dict[str, Any]], market: float) -> list[dict[str, Any]]:
    """ASK pools with liquidity still above mid (upper > mid)."""
    return [
        r
        for r in rows
        if r["side"] == "ASK" and float(r["upper_edge"]) > market
    ]


def _tie_key(r: dict[str, Any]) -> tuple:
    return (
        float(r["lower_edge"]),
        int(r["tf_duration_s"]),
        str(r["pool_id"]),
    )


def select_next_pool(
    inventory: list[dict[str, Any]], *, market_price: float
) -> dict[str, Any]:
    """Deterministic next ASK target. Outcomes never used."""
    above = ask_entirely_above(inventory, market_price)
    containing = ask_containing(inventory, market_price)
    ceiling = ask_remaining_ceiling(inventory, market_price)

    excluded: list[dict[str, Any]] = []
    for r in inventory:
        if r["side"] != "ASK":
            excluded.append(
                {
                    "pool_id": r["pool_id"],
                    "source_timeframe": r["source_timeframe"],
                    "reason": "NOT_ASK",
                }
            )
        elif float(r["upper_edge"]) <= market_price:
            excluded.append(
                {
                    "pool_id": r["pool_id"],
                    "source_timeframe": r["source_timeframe"],
                    "reason": "ENTIRELY_BELOW_OR_AT_MARKET",
                    "upper_edge": r["upper_edge"],
                }
            )

    selection_mode: str
    candidates: list[dict[str, Any]]
    if above:
        selection_mode = "STRICT_ASK_ABOVE_MARKET_FRONT_EDGE"
        candidates = []
        for r in above:
            c = dict(r)
            c["selection_distance_bps"] = float(r["distance_to_front_edge_bps"])
            c["target_edge"] = float(r["lower_edge"])
            c["target_edge_role"] = "FRONT_EDGE_LOWER"
            candidates.append(c)
        candidates.sort(
            key=lambda x: (x["selection_distance_bps"],) + _tie_key(x)
        )
    elif ceiling:
        # Market already inside HTF ASK after CASE_02 breakout — next boundary above
        # is the nearest remaining back edge (upper).
        selection_mode = "INSIDE_ASK_REMAINING_BACK_EDGE"
        candidates = []
        for r in ceiling:
            c = dict(r)
            dist = bps(float(r["upper_edge"]), market_price)
            if dist <= 0:
                continue
            c["selection_distance_bps"] = dist
            c["target_edge"] = float(r["upper_edge"])
            c["target_edge_role"] = "BACK_EDGE_UPPER"
            candidates.append(c)
        candidates.sort(
            key=lambda x: (x["selection_distance_bps"],) + _tie_key(x)
        )
        for r in inventory:
            if r["side"] == "ASK" and float(r["lower_edge"]) > market_price:
                continue  # already in above (empty)
    else:
        selection_mode = "NO_ASK_LIQUIDITY_ABOVE_MARKET"
        candidates = []

    selected = candidates[0] if candidates else None
    component: list[dict[str, Any]] = []
    htf_confluence: list[dict[str, Any]] = []
    if selected is not None:
        slo, shi = float(selected["lower_edge"]), float(selected["upper_edge"])
        for r in inventory:
            if r["side"] != "ASK":
                continue
            if r["pool_id"] == selected["pool_id"] and r["source_timeframe"] == selected[
                "source_timeframe"
            ]:
                continue
            if intervals_overlap(slo, shi, float(r["lower_edge"]), float(r["upper_edge"])):
                component.append(
                    {
                        "pool_id": r["pool_id"],
                        "source_timeframe": r["source_timeframe"],
                        "lower_edge": r["lower_edge"],
                        "upper_edge": r["upper_edge"],
                        "strength": r.get("strength"),
                        "available_at": r.get("available_at"),
                    }
                )
                if TF_DURATION_S[r["source_timeframe"]] > TF_DURATION_S[
                    selected["source_timeframe"]
                ]:
                    htf_confluence.append(
                        {
                            "pool_id": r["pool_id"],
                            "source_timeframe": r["source_timeframe"],
                            "lower_edge": r["lower_edge"],
                            "upper_edge": r["upper_edge"],
                            "role": "HTF_CONFLUENCE",
                        }
                    )

        # If selected is 5m inside HTF, keep 5m as target (rule); already satisfied by sort.
        # If selected is HTF because no 5m above, document that.
        if selected["source_timeframe"] == "5m" and htf_confluence:
            selected = dict(selected)
            selected["htf_note"] = (
                "5m remains first target; higher TFs documented as HTF_CONFLUENCE"
            )

    # Non-selected candidates explicitly excluded
    for c in candidates[1:]:
        excluded.append(
            {
                "pool_id": c["pool_id"],
                "source_timeframe": c["source_timeframe"],
                "reason": "NOT_NEAREST_POSITIVE_DISTANCE",
                "selection_distance_bps": c["selection_distance_bps"],
                "target_edge": c["target_edge"],
            }
        )

    manifest = {
        "format_version": "pool_selection_manifest/v1",
        "symbol": SYMBOL,
        "reference_ts": REF_TS,
        "market_price_at_reference": market_price,
        "selection_mode": selection_mode,
        "primary_rules": [
            "only_active_confirmed_ASK",
            "prefer_strict_lower_edge_above_market",
            "else_inside_ASK_nearest_remaining_upper_edge",
            "smallest_positive_distance",
            "overlap_documented_as_component",
            "tie_break: lower_edge, timeframe_duration, pool_id",
        ],
        "not_used_for_selection": [
            "later_touch",
            "later_rejection",
            "later_breakout",
            "return",
            "mfe_mae",
            "chart_attractiveness",
        ],
        "outcome_used_for_pool_selection": OUTCOME_USED_FOR_POOL_SELECTION,
        "outcome_used_for_matching": OUTCOME_USED_FOR_MATCHING,
        "outcome_used_for_thresholds": OUTCOME_USED_FOR_THRESHOLDS,
        "outcome_used_for_state_definition": OUTCOME_USED_FOR_STATE_DEFINITION,
        "n_inventory_active": len(inventory),
        "n_ask_entirely_above": len(above),
        "n_ask_containing": len(containing),
        "n_ask_remaining_ceiling": len(ceiling),
        "n_candidates_ranked": len(candidates),
        "selected_pool_id": None if selected is None else selected["pool_id"],
        "selected_source_timeframe": None
        if selected is None
        else selected["source_timeframe"],
        "component_pool_ids": [c["pool_id"] for c in component],
        "htf_confluence": htf_confluence,
        "excluded_candidates": excluded,
        "ranked_candidate_distances": [
            {
                "pool_id": c["pool_id"],
                "source_timeframe": c["source_timeframe"],
                "selection_distance_bps": c["selection_distance_bps"],
                "target_edge": c["target_edge"],
                "target_edge_role": c["target_edge_role"],
                "lower_edge": c["lower_edge"],
                "upper_edge": c["upper_edge"],
            }
            for c in candidates
        ],
    }
    return {"manifest": manifest, "selected": selected, "component": component}
