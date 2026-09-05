"""Phase 2A.3 fight sequence validation orchestrator."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .aggression_facts import aggression_for_trades
from .config import iso_z, utc
from .edge_book_coverage import build_edge_book_coverage
from .edge_observability import build_edge_observability
from .edge_region_consumption import (
    build_edge_region_consumption,
    build_exact_refill_events,
    build_nearby_liquidity_increases,
)
from .edge_regions import build_edge_region_catalog
from .edge_visits import build_edge_visits
from .facts import json_safe
from .fight_cluster_sensitivity import SENSITIVITY_LABEL, build_fight_cluster_sensitivity
from .edge_visit_cluster_audit import build_edge_visit_cluster_join_audit
from .first_outside_bin_contract import build_first_outside_bin_contract
from .outside_reclaim import (
    RECLAIM_EVENT_CONTRACT_V3,
    assert_invariants_or_raise,
    run_outside_reclaim_invariant_audit,
)
from .phase_2a2_preflight import build_phase_2a2_preflight
from .phase_2a3_preflight import build_phase_2a3_preflight
from .same_timestamp_audit import build_same_timestamp_ordering_audit
from .sequence_metrics import (
    build_consumption_metrics,
    build_coverage_aware_consumption_metrics,
    build_nearby_liquidity_metrics,
    build_ob_coverage_metrics,
    build_outside_excursion_category_metrics,
    build_refill_metrics,
)
from .preflight_audit import build_preflight_audit
from .profile_price_bin_contract import build_profile_price_bin_contract

SEQUENCE_VALIDATION_CONTRACT = "fight_sequence_validation_v3"
SCHEMA_SEQUENCE = "btc_ob_fight_sequence_v2_3"
INTERPRETATION_NOT_EVALUATED = "NOT_EVALUATED"

VERDICT_READY = "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_READY"
VERDICT_PARTIAL = "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_PARTIAL"
VERDICT_BLOCKED = "BTC_OB_FIGHT_CANONICAL_ELIGIBILITY_BLOCKED"


def build_sequence_validation(
    *,
    tpo_profile: dict[str, Any],
    volume_profile: dict[str, Any],
    fight_bundle: dict[str, Any],
    wall_bundle: dict[str, Any],
    ob_rows: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    anchor: datetime,
    window_end: datetime,
    trades_meta: dict[str, Any] | None = None,
    strict_invariants: bool = True,
) -> dict[str, Any]:
    """Build Phase 2A.1 sequence and edge-region validation bundle."""
    anchor = utc(anchor)
    window_end = utc(window_end)
    edges = fight_bundle.get("frozen_profile_edges") or {}
    episodes = fight_bundle.get("profile_state_episodes") or []
    transitions = fight_bundle.get("profile_state_transitions") or []
    phase2a_reclaims = None  # legacy bug path removed for new runs

    episode_bundle = {
        "episodes": episodes,
        "transitions": transitions,
        "observation_start_utc": iso_z(anchor),
        "observation_end_utc": iso_z(window_end),
        "trade_count_observed": len([t for t in trades if anchor <= t["ts"] < window_end]),
    }

    preflight, episode_distribution = build_preflight_audit(
        episode_bundle,
        phase2a_reclaims=None,
        edge_consumption=fight_bundle.get("edge_consumption_events"),
        trades_meta=trades_meta,
    )

    preflight_2a2 = build_phase_2a2_preflight()
    preflight_2a3 = build_phase_2a3_preflight()

    bin_contract = build_profile_price_bin_contract(tpo_profile, volume_profile, edges=edges)
    first_outside_contract = build_first_outside_bin_contract(tpo_profile, volume_profile, edges)
    region_catalog = build_edge_region_catalog(tpo_profile, volume_profile, edges)

    visits = fight_bundle.get("edge_visits") or build_edge_visits(episodes, window_end=window_end)
    excursions = fight_bundle.get("outside_excursions") or []
    reclaims = fight_bundle.get("reclaim_events") or []
    ambiguous_candidates = fight_bundle.get("ambiguous_reclaim_candidates") or []
    eligibility_summary = fight_bundle.get("canonical_eligibility_summary") or {}
    raw_excursions = fight_bundle.get("raw_outside_excursions") or excursions
    canonical_excursions = fight_bundle.get("canonical_outside_excursions") or []
    ambiguous_excursions = fight_bundle.get("ambiguous_same_timestamp_excursions") or []

    st_audit = fight_bundle.get("same_timestamp_ordering_audit")
    st_rows = fight_bundle.get("same_timestamp_multistate_groups") or []
    if st_audit is None:
        st_audit, st_rows = build_same_timestamp_ordering_audit(
            trades, edges, anchor=anchor, window_end=window_end, trades_meta=trades_meta
        )

    _attach_reclaims_to_visits(visits, excursions, reclaims)

    sensitivity_rows, clusters_by_gap = build_fight_cluster_sensitivity(visits, episodes)
    join_audit = build_edge_visit_cluster_join_audit(visits, episodes, clusters_by_gap)

    gap0_count = next((r["cluster_count"] for r in sensitivity_rows if r["max_inside_gap_seconds"] == 0), 0)
    gap0_invariant_ok = gap0_count == len(visits)

    # Precompute OB tick maps once for coverage + observability.
    from .edge_observability import _prepare_ob_rows

    prepared_ob = _prepare_ob_rows(ob_rows)
    book_coverage, depth_samples, book_summary = build_edge_book_coverage(prepared_ob, region_catalog)
    consumption, consumption_summary = build_edge_region_consumption(
        wall_bundle,
        region_catalog,
        edges,
        visits,
        excursions,
        episodes,
        book_coverage,
    )
    exact_refills = build_exact_refill_events(consumption, wall_bundle)
    nearby_increases = build_nearby_liquidity_increases(consumption, wall_bundle, region_catalog)

    invariant_audit = run_outside_reclaim_invariant_audit(
        excursions,
        reclaims,
        phase2a_reclaims=None,
        ambiguous_candidates=ambiguous_candidates,
        canonical_outside_count=len(canonical_excursions),
    )
    if not gap0_invariant_ok:
        invariant_audit["passed"] = False
        invariant_audit.setdefault("violations", []).append(
            {
                "type": "GAP0_CLUSTER_COUNT_MISMATCH",
                "edge_visit_count": len(visits),
                "cluster_count_gap_0": gap0_count,
            }
        )
    if strict_invariants:
        assert_invariants_or_raise(invariant_audit)

    obs_trades = sorted(
        [t for t in trades if anchor <= t["ts"] < window_end],
        key=lambda t: (t["ts"], t["trade_id"]),
    )
    aggression = _build_sequence_aggression(visits, excursions, reclaims, obs_trades)
    oi_liq = _build_visit_excursion_oi_liq(visits, excursions, oi_rows, liq_rows)

    for v in visits:
        v["oi_liquidation_context"] = oi_liq.get("by_visit", {}).get(v["edge_visit_id"], {})
    for e in excursions:
        e["oi_liquidation_context"] = oi_liq.get("by_excursion", {}).get(e["outside_excursion_id"], {})

    ob_metrics = build_ob_coverage_metrics(book_coverage)
    consumption_metrics = build_consumption_metrics(consumption)
    coverage_aware_consumption = build_coverage_aware_consumption_metrics(consumption, visits)
    nearby_metrics = build_nearby_liquidity_metrics(nearby_increases)
    refill_metrics = build_refill_metrics(exact_refills, nearby_increases)
    outside_category_metrics = build_outside_excursion_category_metrics(
        raw_excursions, canonical_excursions, ambiguous_excursions
    )

    obs_detail, obs_summary = build_edge_observability(
        prepared_ob,
        region_catalog,
        visits,
        excursions,
        reclaims,
        episodes,
        window_start=anchor,
        window_end=window_end,
    )

    verdict = _compute_verdict(
        invariant_audit, bin_contract, book_summary, edges, gap0_invariant_ok, eligibility_summary
    )

    edge_visit_count = len(visits)
    summary = {
        "verdict": verdict,
        "schema_version": SCHEMA_SEQUENCE,
        "sequence_validation_contract": SEQUENCE_VALIDATION_CONTRACT,
        "canonical_reclaim_contract": RECLAIM_EVENT_CONTRACT_V3,
        "canonical_reclaims_only_in_primary_output": True,
        "ambiguous_events_decision_eligible": False,
        "exchange_order_proven": False,
        "raw_outside_observation_count": len(raw_excursions),
        "ambiguous_reclaim_candidate_count": len(ambiguous_candidates),
        "canonical_outside_count": len(canonical_excursions),
        "canonical_reclaim_count": len(reclaims),
        "interpretation_status": INTERPRETATION_NOT_EVALUATED,
        "rules_frozen": False,
        "trade_verdict_evaluated": False,
        "direction": None,
        "legacy_global_first_reclaim_enabled": False,
        "same_timestamp_ordering_audited": True,
        "raw_state_episode_count": len(episodes),
        "edge_visit_count": edge_visit_count,
        "edge_visits_upper": sum(1 for v in visits if v["edge"] == "UPPER"),
        "edge_visits_lower": sum(1 for v in visits if v["edge"] == "LOWER"),
        "cluster_count_gap_0": gap0_count,
        "gap0_invariant_ok": gap0_invariant_ok,
        "outside_excursion_count_raw": len(raw_excursions),
        "outside_excursion_count_canonical": len(canonical_excursions),
        "outside_excursion_count_ambiguous": len(ambiguous_excursions),
        "reclaim_count": len(reclaims),
        "ambiguous_candidate_count": len(ambiguous_candidates),
        "unique_reclaim_cross_timestamps": len({r.get("cross_ts") for r in reclaims}),
        "open_excursion_count": sum(1 for e in excursions if not e.get("reclaimed")),
        "cluster_counts_by_gap": {str(r["max_inside_gap_seconds"]): r["cluster_count"] for r in sensitivity_rows},
        "consumption_by_scope": consumption_summary.get("by_scope"),
        "consumption_metrics": consumption_metrics,
        "ob_coverage_metrics": ob_metrics,
        "refill_metrics": refill_metrics,
        "outside_excursion_metrics": outside_category_metrics,
        "exact_refill_count": len(exact_refills),
        "nearby_liquidity_increase_count": len(nearby_increases),
        "nearby_ask_count": nearby_metrics.get("ask_count", 0),
        "nearby_bid_count": nearby_metrics.get("bid_count", 0),
        "nearby_unknown_count": nearby_metrics.get("unknown_count", 0),
        "coverage_aware_consumption_metrics": coverage_aware_consumption,
        "edge_observability_summary": obs_summary,
        "edge_book_coverage": book_summary,
        "oi_liquidation_coverage": oi_liq.get("coverage_summary"),
        "historical_run_012_reclaim_status": "KNOWN_INVALID_GLOBAL_FIRST_BUG",
        "sensitivity_status": SENSITIVITY_LABEL,
    }

    return json_safe(
        {
            "schema_version": SCHEMA_SEQUENCE,
            "sequence_validation_contract": SEQUENCE_VALIDATION_CONTRACT,
            "verdict": verdict,
            "interpretation_status": INTERPRETATION_NOT_EVALUATED,
            "rules_frozen": False,
            "preflight_audit": preflight,
            "episode_distribution": episode_distribution,
            "profile_price_bin_contract": bin_contract,
            "edge_region_catalog": region_catalog,
            "edge_visits": visits,
            "fight_cluster_sensitivity": sensitivity_rows,
            "fight_clusters_by_gap": clusters_by_gap,
            "edge_book_coverage": book_coverage,
            "edge_region_depth_samples": depth_samples,
            "edge_book_coverage_summary": book_summary,
            "edge_region_consumption_events": consumption,
            "edge_region_consumption_summary": consumption_summary,
            "exact_refill_events": exact_refills,
            "nearby_liquidity_increase_events": nearby_increases,
            "outside_excursions": excursions,
            "reclaim_events": reclaims,
            "outside_reclaim_invariant_audit": invariant_audit,
            "fight_sequence_aggression": aggression,
            "fight_sequence_summary": summary,
            "phase_2a2_consistency_preflight": preflight_2a2,
            "phase_2a3_preflight_audit": preflight_2a3,
            "same_timestamp_ordering_audit": st_audit,
            "same_timestamp_multistate_groups": st_rows,
            "edge_visit_cluster_join_audit": join_audit,
            "raw_outside_excursions": raw_excursions,
            "ambiguous_reclaim_candidates": ambiguous_candidates,
            "canonical_eligibility_summary": eligibility_summary,
            "canonical_outside_excursions": canonical_excursions,
            "ambiguous_same_timestamp_excursions": ambiguous_excursions,
            "outside_excursion_category_metrics": outside_category_metrics,
            "ob_coverage_metrics": ob_metrics,
            "consumption_metrics_detail": consumption_metrics,
            "coverage_aware_consumption_metrics": coverage_aware_consumption,
            "refill_metrics_detail": refill_metrics,
            "nearby_liquidity_increase_metrics": nearby_metrics,
            "edge_observability_detail": obs_detail,
            "edge_observability_summary": obs_summary,
            "first_outside_bin_contract": first_outside_contract,
        }
    )


def _attach_reclaims_to_visits(
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
    reclaims: list[dict[str, Any]],
) -> None:
    exc_by_ep = {e["source_episode_id"]: e for e in excursions}
    rcl_by_exc = {r["outside_excursion_id"]: r for r in reclaims if r.get("outside_excursion_id")}
    for v in visits:
        count = 0
        for eid in v.get("raw_episode_ids") or []:
            exc = exc_by_ep.get(eid)
            if exc and exc.get("reclaimed"):
                count += 1
        v["reclaim_count"] = count


def _build_sequence_aggression(
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
    reclaims: list[dict[str, Any]],
    trades: list[dict[str, Any]],
) -> dict[str, Any]:
    out: dict[str, Any] = {"by_visit": [], "by_excursion": [], "by_reclaim": []}
    for v in visits:
        start = datetime.fromisoformat(v["start_ts"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(v["end_ts"].replace("Z", "+00:00"))
        chunk = [t for t in trades if start <= t["ts"] <= end]
        out["by_visit"].append(
            {
                "edge_visit_id": v["edge_visit_id"],
                "phase": "full_visit",
                **aggression_for_trades(chunk),
            }
        )
    for exc in excursions:
        start = datetime.fromisoformat(exc["start_ts"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(exc["end_ts"].replace("Z", "+00:00"))
        chunk = [t for t in trades if start <= t["ts"] <= end]
        out["by_excursion"].append(
            {
                "outside_excursion_id": exc["outside_excursion_id"],
                "phase": "outside_excursion",
                **aggression_for_trades(chunk),
            }
        )
    for r in reclaims:
        cross = datetime.fromisoformat(r["cross_ts"].replace("Z", "+00:00"))
        post = [t for t in trades if t["ts"] >= cross][:100]
        out["by_reclaim"].append(
            {
                "reclaim_event_id": r["reclaim_event_id"],
                "phase": "post_reclaim_sample",
                **aggression_for_trades(post),
            }
        )
    return out


def _build_visit_excursion_oi_liq(
    visits: list[dict[str, Any]],
    excursions: list[dict[str, Any]],
    oi_rows: list[dict[str, Any]],
    liq_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    by_visit: dict[str, Any] = {}
    by_excursion: dict[str, Any] = {}
    insufficient = 0

    def ctx(start_ts: str, end_ts: str) -> dict[str, Any]:
        nonlocal insufficient
        start = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_ts.replace("Z", "+00:00"))
        oi_slice = [r for r in oi_rows if start <= r["ts"] <= end]
        liq_slice = [r for r in liq_rows if start <= r["ts"] <= end]
        if not oi_slice and not liq_slice:
            insufficient += 1
            return {"context_status": "DATA_INSUFFICIENT"}
        oi_first = oi_slice[0]["oi"] if oi_slice else None
        oi_last = oi_slice[-1]["oi"] if oi_slice else None
        oi_delta = (oi_last - oi_first) if oi_first is not None and oi_last is not None else None
        long_liq = [r for r in liq_slice if str(r.get("side", "")).upper().startswith(("BUY", "LONG"))]
        short_liq = [r for r in liq_slice if str(r.get("side", "")).upper().startswith(("SELL", "SHORT"))]
        largest = max(liq_slice, key=lambda r: r.get("notional", 0), default=None)
        return {
            "context_status": "COMPUTED",
            "oi_start": oi_first,
            "oi_end": oi_last,
            "oi_delta": oi_delta,
            "oi_delta_pct": (oi_delta / oi_first * 100.0) if oi_first and oi_delta is not None else None,
            "oi_coverage": len(oi_slice),
            "long_liquidation_count": len(long_liq),
            "short_liquidation_count": len(short_liq),
            "long_liquidation_notional": sum(r.get("notional", 0) for r in long_liq) or None,
            "short_liquidation_notional": sum(r.get("notional", 0) for r in short_liq) or None,
            "largest_liquidation_notional": largest.get("notional") if largest else None,
        }

    for v in visits:
        by_visit[v["edge_visit_id"]] = ctx(v["start_ts"], v["end_ts"])
    for e in excursions:
        by_excursion[e["outside_excursion_id"]] = ctx(e["start_ts"], e["end_ts"])

    return {
        "by_visit": by_visit,
        "by_excursion": by_excursion,
        "coverage_summary": {
            "visit_count": len(visits),
            "excursion_count": len(excursions),
            "data_insufficient_count": insufficient,
        },
    }


def _compute_verdict(
    audit: dict[str, Any],
    bin_contract: dict[str, Any],
    book_summary: dict[str, Any],
    edges: dict[str, Any],
    gap0_ok: bool = True,
    eligibility_summary: dict[str, Any] | None = None,
) -> str:
    if not audit.get("passed") or not gap0_ok:
        return VERDICT_BLOCKED
    if edges.get("profile_state") != "VALID":
        return VERDICT_BLOCKED
    es = eligibility_summary or {}
    if not es.get("invariant_canonical_reclaim_lte_canonical_outside", True):
        return VERDICT_BLOCKED
    if not es.get("invariant_raw_equals_canonical_plus_ambiguous", True):
        return VERDICT_PARTIAL
    if not bin_contract.get("price_step"):
        return VERDICT_PARTIAL
    if not book_summary.get("sample_count"):
        return VERDICT_PARTIAL
    return VERDICT_READY
