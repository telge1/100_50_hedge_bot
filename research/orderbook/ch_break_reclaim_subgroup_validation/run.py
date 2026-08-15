"""Orchestrate subgroup validation audit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from research.orderbook.ch_break_reclaim_subgroup_validation.analysis import (
    classify_subgroup,
    decide_primary,
    distance_baseline_comparison,
    early_signal_results,
    jackknife_table,
    run_subgroup_feature_stats,
    symbol_transfer,
)
from research.orderbook.ch_break_reclaim_subgroup_validation.load import (
    EARLY_TIMEPOINTS,
    load_valid_feature_rows,
    subgroup_outcome_counts,
)
from research.orderbook.ch_break_reclaim_subgroup_validation.report import (
    write_csv,
    write_report,
    write_summary,
)

DEFAULT_OUT = Path(
    "/home/telgenbuescher/projects/spread_recovery_hedge_short_dev/"
    "results/ch_break_reclaim_subgroup_validation_20260808"
)


def run_subgroup_validation(*, out_dir: Path = DEFAULT_OUT) -> dict[str, Any]:
    rows, events = load_valid_feature_rows()
    counts = subgroup_outcome_counts(events)
    feature_stats = run_subgroup_feature_stats(rows)
    distance_rows = distance_baseline_comparison(rows)
    early_rows = early_signal_results(rows)
    jackknife_rows = jackknife_table(rows)
    transfer_rows = symbol_transfer(rows)

    # Merge early univariate + scorecard for classification
    early_for_class = list(early_rows)
    for r in feature_stats:
        if r["timepoint"] in EARLY_TIMEPOINTS and r.get("sufficient_sample"):
            early_for_class.append(r)

    classifications = []
    for c in counts:
        classifications.append(
            classify_subgroup(
                counts=c,
                early_rows=early_for_class,
                distance_rows=distance_rows,
                jackknife_rows=jackknife_rows,
            )
        )

    primary = decide_primary(classifications, transfer_rows)

    # strongest subgroup among candidates else best AUC
    candidates = [c for c in classifications if c["classification"] == "EARLY_GATE_CANDIDATE"]
    if candidates:
        strongest = max(candidates, key=lambda c: c.get("best_early_auc") or 0)
    else:
        scored = [c for c in classifications if c.get("best_early_auc") is not None]
        strongest = max(scored, key=lambda c: c.get("best_early_auc") or 0) if scored else None

    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "subgroup_counts.csv", counts)
    write_csv(out_dir / "subgroup_feature_stats.csv", feature_stats)
    write_csv(out_dir / "distance_baseline_comparison.csv", distance_rows)
    write_csv(out_dir / "early_signal_results.csv", early_rows)
    write_csv(out_dir / "jackknife_stability.csv", jackknife_rows)
    write_csv(out_dir / "symbol_transfer.csv", transfer_rows)
    write_csv(out_dir / "subgroup_classifications.csv", classifications)

    summary = {
        "primary_decision": primary,
        "n_events_data_valid_non_excluded": len(events),
        "subgroup_classifications": classifications,
        "strongest_subgroup": strongest,
        "artifact_dir": str(out_dir),
    }
    write_summary(out_dir / "summary.json", summary)
    write_report(
        out_dir,
        primary=primary,
        counts=counts,
        classifications=classifications,
        distance_rows=distance_rows,
        early_rows=early_rows,
        jackknife_rows=jackknife_rows,
        transfer_rows=transfer_rows,
        strongest=strongest,
    )
    return summary
