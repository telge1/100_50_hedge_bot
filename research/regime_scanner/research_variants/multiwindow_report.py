"""Generate multi-window comparison artifacts."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from research.regime_scanner.research_variants.aggregate import (
    ROBUSTNESS_DEGENERATE_PENALTY,
    ROBUSTNESS_MEDIAN_WEIGHT,
    ROBUSTNESS_STDDEV_PENALTY,
    ROBUSTNESS_WORST_PENALTY,
)
from research.regime_scanner.research_variants.windows import (
    ResearchWindowSet,
    window_hash,
    window_set_hash,
    window_set_json,
)

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results_research_variants_multiwindow"


def write_multiwindow_report(
    *,
    variant_set_name: str,
    window_set_name: str,
    window_set: ResearchWindowSet,
    rows: list[dict[str, Any]],
    aggregates: dict[str, dict[str, Any]],
    plausibility: dict[str, Any],
) -> dict[str, Path]:
    out = RESULTS_DIR
    out.mkdir(parents=True, exist_ok=True)

    windows_path = out / f"{window_set_name}_windows.json"
    by_window_path = out / f"{variant_set_name}_by_window.csv"
    aggregate_path = out / f"{variant_set_name}_aggregate_ranking.csv"
    metric_matrix_path = out / f"{variant_set_name}_metric_matrix.csv"
    rank_matrix_path = out / f"{variant_set_name}_rank_matrix.csv"
    baseline_deltas_path = out / f"{variant_set_name}_baseline_deltas.csv"
    summary_path = out / f"{variant_set_name}_summary.json"

    windows_payload = {
        "window_set": window_set_name,
        "window_set_hash": window_set_hash(window_set),
        "windows": [
            {
                **w.to_canonical_dict(),
                "window_hash": window_hash(w),
            }
            for w in window_set.windows
        ],
    }
    windows_path.write_text(json.dumps(windows_payload, indent=2, default=str), encoding="utf-8")

    by_window_fields = [
        "window",
        "expected_character",
        "variant",
        "run_id",
        "reused",
        "status",
        "score",
        "rank",
        "degenerate",
        "state_changes",
        "short_state_runs",
        "transition_share",
        "structure_conflicts",
        "detected_turns",
        "median_state_duration",
        "runtime_seconds",
    ]
    with by_window_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=by_window_fields)
        writer.writeheader()
        for r in sorted(rows, key=lambda x: (x.get("window_name", ""), x.get("variant_name", ""))):
            m = r.get("stability_metrics") or {}
            writer.writerow(
                {
                    "window": r.get("window_name"),
                    "expected_character": r.get("expected_character"),
                    "variant": r.get("variant_name"),
                    "run_id": r.get("run_id"),
                    "reused": r.get("reused"),
                    "status": r.get("status"),
                    "score": r.get("score"),
                    "rank": r.get("rank"),
                    "degenerate": m.get("degenerate"),
                    "state_changes": m.get("state_change_count"),
                    "short_state_runs": m.get("short_state_run_count"),
                    "transition_share": m.get("transition_share"),
                    "structure_conflicts": m.get("trend_structure_conflict_count"),
                    "detected_turns": m.get("detected_turn_count"),
                    "median_state_duration": m.get("median_state_duration_bars"),
                    "runtime_seconds": r.get("runtime_seconds"),
                }
            )

    agg_fields = [
        "variant",
        "mean_score",
        "median_score",
        "min_score",
        "max_score",
        "score_stddev",
        "mean_rank",
        "median_rank",
        "worst_rank",
        "top_1_count",
        "top_2_count",
        "degenerate_windows",
        "robustness_score",
    ]
    agg_sorted = sorted(
        aggregates.items(),
        key=lambda kv: (-(kv[1].get("robustness_score") or -999), kv[0]),
    )
    with aggregate_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=agg_fields)
        writer.writeheader()
        for variant, agg in agg_sorted:
            writer.writerow(
                {
                    "variant": variant,
                    "mean_score": agg.get("mean_score"),
                    "median_score": agg.get("median_score"),
                    "min_score": agg.get("min_score"),
                    "max_score": agg.get("max_score"),
                    "score_stddev": agg.get("score_stddev"),
                    "mean_rank": agg.get("mean_rank"),
                    "median_rank": agg.get("median_rank"),
                    "worst_rank": agg.get("worst_rank"),
                    "top_1_count": agg.get("top_1_count"),
                    "top_2_count": agg.get("top_2_count"),
                    "degenerate_windows": agg.get("degenerate_window_count"),
                    "robustness_score": agg.get("robustness_score"),
                }
            )

    windows = sorted({r.get("window_name") for r in rows})
    variants = sorted({r.get("variant_name") for r in rows})
    with metric_matrix_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["variant", *windows])
        writer.writeheader()
        for variant in variants:
            row_out: dict[str, Any] = {"variant": variant}
            for w in windows:
                match = next(
                    (r for r in rows if r.get("variant_name") == variant and r.get("window_name") == w),
                    None,
                )
                row_out[w] = match.get("score") if match else None
            writer.writerow(row_out)

    with rank_matrix_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["variant", *windows])
        writer.writeheader()
        for variant in variants:
            row_out = {"variant": variant}
            for w in windows:
                match = next(
                    (r for r in rows if r.get("variant_name") == variant and r.get("window_name") == w),
                    None,
                )
                row_out[w] = match.get("rank") if match else None
            writer.writerow(row_out)

    delta_fields = ["window", "variant", "delta_score", "delta_state_changes", "delta_transition_share"]
    with baseline_deltas_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=delta_fields)
        writer.writeheader()
        for r in rows:
            if r.get("variant_name") == "baseline":
                continue
            deltas = r.get("baseline_deltas") or {}
            writer.writerow(
                {
                    "window": r.get("window_name"),
                    "variant": r.get("variant_name"),
                    "delta_score": deltas.get("delta_score"),
                    "delta_state_changes": deltas.get("delta_state_change_count"),
                    "delta_transition_share": deltas.get("delta_transition_share"),
                }
            )

    most_stable = agg_sorted[0][0] if agg_sorted else None
    summary = {
        "variant_set": variant_set_name,
        "window_set": window_set_name,
        "window_set_hash": window_set_hash(window_set),
        "run_count": len(rows),
        "most_stable_variant_in_multiwindow_test": most_stable,
        "robustness_weights": {
            "median_weight": ROBUSTNESS_MEDIAN_WEIGHT,
            "stddev_penalty": ROBUSTNESS_STDDEV_PENALTY,
            "worst_penalty": ROBUSTNESS_WORST_PENALTY,
            "degenerate_penalty": ROBUSTNESS_DEGENERATE_PENALTY,
        },
        "aggregates": aggregates,
        "plausibility": plausibility,
        "interpretation_limits": [
            "Results describe stability across selected windows only.",
            "No profit or live-deployment conclusions.",
            "Windows were fixed before full variant ranking.",
        ],
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    return {
        "windows_json": windows_path,
        "by_window_csv": by_window_path,
        "aggregate_ranking_csv": aggregate_path,
        "metric_matrix_csv": metric_matrix_path,
        "rank_matrix_csv": rank_matrix_path,
        "baseline_deltas_csv": baseline_deltas_path,
        "summary_json": summary_path,
    }
