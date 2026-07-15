"""Generate comparison artifacts for variant sets."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results_research_variants"


def write_variant_set_report(
    *,
    variant_set_name: str,
    rows: list[dict[str, Any]],
    baseline_row: dict[str, Any] | None,
) -> dict[str, Path]:
    out = RESULTS_DIR
    out.mkdir(parents=True, exist_ok=True)
    prefix = variant_set_name
    summary_path = out / f"{prefix}_summary.json"
    ranking_path = out / f"{prefix}_ranking.csv"
    metrics_path = out / f"{prefix}_metrics.csv"
    diff_path = out / f"{prefix}_parameter_diff.csv"

    summary = {
        "variant_set": variant_set_name,
        "variant_count": len(rows),
        "ranking": [
            {
                "rank": r.get("rank_position"),
                "variant": r.get("variant_name"),
                "score": r.get("score"),
                "degenerate": r.get("degenerate"),
                "run_id": r.get("run_id"),
                "parameter_hash": r.get("parameter_hash"),
            }
            for r in rows
        ],
        "baseline_reference_run_id": baseline_row.get("run_id") if baseline_row else None,
    }
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    ranking_fields = [
        "variant",
        "parameter_hash",
        "run_id",
        "status",
        "score",
        "degenerate",
        "state_changes",
        "direction_changes",
        "short_state_runs",
        "reversal_within_3_bars",
        "transition_share",
        "avg_state_duration",
        "structure_conflicts",
        "detected_turns",
        "avg_bars_choch_to_trend",
        "runtime_seconds",
        "rank_position",
    ]
    with ranking_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ranking_fields)
        writer.writeheader()
        for r in rows:
            m = r.get("stability_metrics") or {}
            writer.writerow(
                {
                    "variant": r.get("variant_name"),
                    "parameter_hash": r.get("parameter_hash"),
                    "run_id": r.get("run_id"),
                    "status": r.get("status"),
                    "score": r.get("score"),
                    "degenerate": m.get("degenerate"),
                    "state_changes": m.get("state_change_count"),
                    "direction_changes": m.get("direction_change_count"),
                    "short_state_runs": m.get("short_state_run_count"),
                    "reversal_within_3_bars": m.get("reversal_within_3_bars_count"),
                    "transition_share": m.get("transition_share"),
                    "avg_state_duration": m.get("average_state_duration_bars"),
                    "structure_conflicts": m.get("trend_structure_conflict_count"),
                    "detected_turns": m.get("detected_turn_count"),
                    "avg_bars_choch_to_trend": m.get("avg_bars_choch_to_new_trend"),
                    "runtime_seconds": r.get("runtime_seconds"),
                    "rank_position": r.get("rank_position"),
                }
            )

    metric_keys = sorted(
        {
            k
            for r in rows
            for k in (r.get("stability_metrics") or {})
            if k not in {"degenerate_reason"}
        }
    )
    with metrics_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["variant", *metric_keys])
        writer.writeheader()
        for r in rows:
            m = r.get("stability_metrics") or {}
            writer.writerow({"variant": r.get("variant_name"), **{k: m.get(k) for k in metric_keys}})

    with diff_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=["variant", "override_path", "override_value", "baseline_delta_metrics"],
        )
        writer.writeheader()
        for r in rows:
            overrides = r.get("parameter_overrides") or {}
            deltas = r.get("baseline_deltas") or {}
            if not overrides:
                writer.writerow(
                    {
                        "variant": r.get("variant_name"),
                        "override_path": "",
                        "override_value": "",
                        "baseline_delta_metrics": json.dumps(deltas, sort_keys=True),
                    }
                )
                continue
            for path, value in sorted(overrides.items()):
                writer.writerow(
                    {
                        "variant": r.get("variant_name"),
                        "override_path": path,
                        "override_value": value,
                        "baseline_delta_metrics": json.dumps(deltas, sort_keys=True),
                    }
                )

    return {
        "summary_json": summary_path,
        "ranking_csv": ranking_path,
        "metrics_csv": metrics_path,
        "parameter_diff_csv": diff_path,
    }
