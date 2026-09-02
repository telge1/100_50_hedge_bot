"""Phase 2A.3 preflight audit — read-only analysis of run_016 contradictions."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def build_phase_2a3_preflight(
    *,
    run_dir: Path | None = None,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Answer preflight questions from run_016 (read-only)."""
    root = repo_root or Path(__file__).resolve().parents[2]
    run = run_dir or root / "results/btc_ob_fight_cases/20260831T190000Z/run_016"

    reclaims = _read_csv(run / "reclaim_events.csv")
    raw_exc = _read_csv(run / "outside_excursions.csv")
    canon_exc = _read_csv(run / "canonical_outside_excursions.csv")
    ambig_exc = _read_csv(run / "ambiguous_same_timestamp_excursions.csv")
    nearby = _read_csv(run / "nearby_liquidity_increase_events.csv")
    consumption = _read_csv(run / "edge_region_consumption_events.csv")
    ob_cov = _read_csv(run / "edge_book_coverage.csv")
    visits = _read_csv(run / "edge_visits.csv")

    reclaim_fields = sorted(reclaims[0].keys()) if reclaims else []
    has_eligible = "canonical_eligible" in reclaim_fields

    ordering_vals = {r.get("ordering_quality") for r in reclaims}
    ambig_as_normal = sum(
        1 for r in reclaims if r.get("ordering_quality") == "SAME_TIMESTAMP_SHARED_CROSS"
    )

    consumers = [
        "research/btc_ob_fight/reporting.py (_write_fight_fact_outputs)",
        "research/btc_ob_fight/fight_sequence.py (build_sequence_validation)",
        "research/btc_ob_fight/fight_facts.py (build_fight_facts manifest)",
        "research/btc_ob_fight/cli.py (analysis_manifest)",
        "dashboard/ (not modified in 2A.3)",
        "research/regime_scanner/* (separate reclaim_events.csv contract)",
    ]

    ob_edges = {}
    ob_scopes = {}
    for r in ob_cov:
        ob_edges[r.get("edge", "?")] = ob_edges.get(r.get("edge", "?"), 0) + 1
        ob_scopes[r.get("scope", "?")] = ob_scopes.get(r.get("scope", "?"), 0) + 1

    upper_visits = sum(1 for v in visits if v.get("edge") == "UPPER")
    lower_visits = sum(1 for v in visits if v.get("edge") == "LOWER")

    cons_with_side = sum(1 for c in consumption if c.get("side"))
    nearby_with_side = sum(1 for n in nearby if n.get("side") in ("ASK", "BID"))

    return {
        "preflight_version": "phase_2a3_eligibility_v1",
        "source_run": str(run),
        "source_run_present": run.is_dir(),
        "questions": {
            "q1_why_117_reclaims_vs_8_canonical": {
                "answer": (
                    "Phase 2A.2 wrote ALL episode-scoped raw reclaims into reclaim_events.csv "
                    "without splitting by canonical_eligible. Excursion categorization (8 canonical / "
                    "109 ambiguous) was computed separately but not applied to reclaim output."
                ),
                "reclaim_count": len(reclaims),
                "canonical_outside_count": len(canon_exc),
                "ambiguous_outside_count": len(ambig_exc),
                "raw_outside_count": len(raw_exc),
            },
            "q2_canonical_eligible_field": {
                "present_in_reclaim_events": has_eligible,
                "fields_present": reclaim_fields,
                "ordering_quality_values": sorted(ordering_vals),
            },
            "q3_ambiguous_called_normal_reclaims": {
                "same_timestamp_shared_reclaim_rows": ambig_as_normal,
                "all_labeled_as_confirmed_reclaims": True,
                "issue": "109 ambiguous excursions still produced reclaim_events.csv rows",
            },
            "q4_consumers_of_reclaim_events_csv": consumers,
            "q5_count_terminology_consistent": {
                "issue": "Report/console used 'Reclaims' for 117 without RAW/AMBIGUOUS/CANONICAL split",
                "outside_excursion_count_raw_in_summary": True,
                "reclaim_count_in_manifest": len(reclaims),
            },
            "q6_ob_coverage_denominator": {
                "method": "Every OB snapshot × every region in catalog (upper+lower × scopes except skipped FIRST_OUTSIDE_BIN)",
                "total_samples": len(ob_cov),
                "formula": "sample_count = len(ob_rows) × len(regions_per_side) × 2 sides",
            },
            "q7_upper_lower_aggregated_together": {
                "yes": True,
                "upper_samples": ob_edges.get("UPPER", 0),
                "lower_samples": ob_edges.get("LOWER", 0),
                "note": "Both edges counted even when only UPPER had visits",
            },
            "q8_samples_outside_edge_visits": {
                "yes": True,
                "method": "FULL_WINDOW coverage — all OB snapshots in observation window, not filtered to visit spans",
            },
            "q9_first_outside_bin_zero_samples": {
                "reason": "edge_book_coverage.build_edge_book_coverage explicitly skips scope FIRST_OUTSIDE_BIN (continue)",
                "first_outside_bin_samples": ob_scopes.get("FIRST_OUTSIDE_BIN", 0),
            },
            "q10_nearby_missing_side": {
                "nearby_count": len(nearby),
                "with_ask_bid_side": nearby_with_side,
                "reason": "build_nearby_liquidity_increases did not copy side from parent consumption event to output row",
            },
            "q11_parent_consumption_has_side": {
                "consumption_events_with_side": cons_with_side,
                "consumption_total": len(consumption),
                "parent_side_available": cons_with_side == len(consumption),
            },
            "q12_side_via_lineage_without_guessing": {
                "feasible": True,
                "method": "Copy side + parent_consumption_event_id from consumption event; no price-direction inference needed",
            },
        },
        "visit_counts": {"upper": upper_visits, "lower": lower_visits},
        "fix_targets": [
            "reclaim_event_contract_v3 split outputs",
            "edge_observability_contract_v1 context-filtered coverage",
            "FIRST_OUTSIDE_BIN scope computation in book coverage",
            "nearby side lineage from parent consumption",
            "coverage-aware consumption metrics",
        ],
    }


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))
