"""Read-only state + score audit built entirely from stored research runs.

Produces the Phase 2-5 artifacts without any scanner execution:

* raw_state_distribution.csv
* raw_state_duration.csv
* raw_state_transition_matrix.csv
* state_bucket_mapping.csv
* score_component_breakdown.csv
* pilot_reevaluation.csv       (Phase 7 corrected ranking, no scanner)
* audit_summary.json
"""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path
from typing import Any

from research.regime_scanner.research_variants.scoring import (
    METRIC_VERSION,
    SCORE_VERSION,
    evaluate_window,
    score_components,
)
from research.regime_scanner.research_variants.stability import compute_stability_metrics
from research.regime_scanner.research_variants.state_buckets import (
    STATE_BUCKET_MAP,
    bucket_reason,
    classify_research_state_bucket,
)

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results_state_metric_audit"

WINDOW_CHARACTER = {
    "transition_march_week": "transition",
    "trend_up_late_feb": "uptrend",
    "trend_down_early_jun": "downtrend",
    "range_late_may": "range",
    "mixed_feb_mar_six_weeks": "mixed",
}


def _run_lengths_by_state(states: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    if not states:
        return out
    cur = states[0]
    n = 1
    for s in states[1:]:
        if s == cur:
            n += 1
        else:
            out.setdefault(cur, []).append(n)
            cur = s
            n = 1
    out.setdefault(cur, []).append(n)
    return out


def _transition_matrix(states: list[str]) -> dict[tuple[str, str], int]:
    mat: dict[tuple[str, str], int] = {}
    for a, b in zip(states, states[1:]):
        if a != b:
            mat[(a, b)] = mat.get((a, b), 0) + 1
    return mat


def audit_run(trend_states: list[dict[str, Any]]) -> dict[str, Any]:
    states = [str(r.get("state") or "") for r in trend_states]
    n = len(states)
    counts: dict[str, int] = {}
    first_idx: dict[str, int] = {}
    last_idx: dict[str, int] = {}
    for i, s in enumerate(states):
        counts[s] = counts.get(s, 0) + 1
        first_idx.setdefault(s, i)
        last_idx[s] = i
    durations = _run_lengths_by_state(states)
    dist = {}
    for s, cc in counts.items():
        lens = durations.get(s, [])
        dist[s] = {
            "count": cc,
            "share": cc / n if n else 0.0,
            "first_index": first_idx.get(s),
            "last_index": last_idx.get(s),
            "avg_duration": float(statistics.mean(lens)) if lens else 0.0,
            "median_duration": float(statistics.median(lens)) if lens else 0.0,
            "bucket": classify_research_state_bucket(s),
        }
    return {
        "bars": n,
        "distribution": dist,
        "transition_matrix": _transition_matrix(states),
        "states": states,
    }


def _resolve_baseline_runs(research_store: Any, variant_store: Any, window_store: Any) -> dict[str, str]:
    v = variant_store.get_variant_set_by_name("simple_regime_stability_v1")
    w = window_store.get_window_set_by_name("regime_market_windows_v1")
    rows = window_store.list_variant_window_runs(
        variant_set_id=int(v["id"]), window_set_id=int(w["id"])
    )
    return {r["window_name"]: str(r["run_id"]) for r in rows if r["variant_name"] == "baseline"}


def _resolve_all_runs(variant_store: Any, window_store: Any) -> list[dict[str, Any]]:
    v = variant_store.get_variant_set_by_name("simple_regime_stability_v1")
    w = window_store.get_window_set_by_name("regime_market_windows_v1")
    return window_store.list_variant_window_runs(
        variant_set_id=int(v["id"]), window_set_id=int(w["id"])
    )


def run_state_metric_audit(research_store: Any, variant_store: Any, window_store: Any) -> dict[str, Any]:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    baselines = _resolve_baseline_runs(research_store, variant_store, window_store)
    all_runs = _resolve_all_runs(variant_store, window_store)

    audits: dict[str, dict[str, Any]] = {}
    for win, rid in baselines.items():
        trend = research_store.load_trend_states(rid)
        audits[win] = {"run_id": rid, **audit_run(trend)}

    # 1. raw_state_distribution.csv
    dist_path = RESULTS_DIR / "raw_state_distribution.csv"
    with dist_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["window", "run_id", "raw_state", "bucket", "count", "share", "first_index", "last_index"])
        for win in sorted(audits):
            a = audits[win]
            for s in sorted(a["distribution"], key=lambda x: -a["distribution"][x]["count"]):
                d = a["distribution"][s]
                wtr.writerow([win, a["run_id"], s, d["bucket"], d["count"], f"{d['share']:.6f}", d["first_index"], d["last_index"]])

    # 2. raw_state_duration.csv
    dur_path = RESULTS_DIR / "raw_state_duration.csv"
    with dur_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["window", "raw_state", "bucket", "avg_duration", "median_duration"])
        for win in sorted(audits):
            a = audits[win]
            for s in sorted(a["distribution"]):
                d = a["distribution"][s]
                wtr.writerow([win, s, d["bucket"], f"{d['avg_duration']:.4f}", f"{d['median_duration']:.4f}"])

    # 3. raw_state_transition_matrix.csv
    mat_path = RESULTS_DIR / "raw_state_transition_matrix.csv"
    with mat_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["window", "from_state", "to_state", "count"])
        for win in sorted(audits):
            a = audits[win]
            for (fr, to), cc in sorted(a["transition_matrix"].items(), key=lambda kv: -kv[1]):
                wtr.writerow([win, fr, to, cc])

    # 4. state_bucket_mapping.csv (canonical mapping + parity per window)
    map_path = RESULTS_DIR / "state_bucket_mapping.csv"
    with map_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["window", "raw_state", "bucket", "count", "share", "mapping_reason"])
        for win in sorted(audits):
            a = audits[win]
            n = a["bars"]
            for s in sorted(a["distribution"], key=lambda x: -a["distribution"][x]["count"]):
                d = a["distribution"][s]
                wtr.writerow([win, s, d["bucket"], d["count"], f"{d['share']:.6f}", bucket_reason(s)])

    # Parity check: sum of bucket counts == number of rows
    parity: dict[str, Any] = {}
    for win, a in audits.items():
        bucket_total = sum(d["count"] for d in a["distribution"].values())
        parity[win] = {
            "rows": a["bars"],
            "bucket_sum": bucket_total,
            "equal": bucket_total == a["bars"],
        }

    # 5. score_component_breakdown.csv (Phase 5) — every variant/window we have
    comp_path = RESULTS_DIR / "score_component_breakdown.csv"
    reeval_rows: list[dict[str, Any]] = []
    with comp_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.writer(fh)
        wtr.writerow(["window", "variant", "component", "raw_value", "weight", "weighted_value"])
        for rec in sorted(all_runs, key=lambda r: (r["window_name"], r["variant_name"])):
            win = rec["window_name"]
            var = rec["variant_name"]
            rid = str(rec["run_id"])
            trend = research_store.load_trend_states(rid)
            struct = research_store.load_structure_events(rid)
            metrics = compute_stability_metrics(trend_states=trend, structure_events=struct)
            comps = score_components(metrics)
            for cname, cd in comps.items():
                wtr.writerow([win, var, cname, f"{cd['raw_value']:.6f}", f"{cd['weight']:.4f}", f"{cd['weighted_value']:.6f}"])
            ev = evaluate_window(
                trend_states=trend,
                structure_events=struct,
                expected_character=WINDOW_CHARACTER.get(win),
            )
            reeval_rows.append(
                {
                    "window": win,
                    "expected_character": WINDOW_CHARACTER.get(win),
                    "variant": var,
                    "run_id": rid,
                    "old_score": rec.get("score"),
                    "raw_component_score": ev["raw_component_score"],
                    "new_stability_score": ev["stability_score"],
                    "degenerate": ev["degenerate"],
                    "degenerate_reason": ev["degenerate_reason"],
                    "rankable": ev["rankable"],
                    "window_character_fit": ev.get("character_fit", {}).get("window_character_fit"),
                }
            )

    # 6. pilot_reevaluation.csv (Phase 7 corrected ranking without scanner)
    reeval_path = RESULTS_DIR / "pilot_reevaluation.csv"
    fields = [
        "window", "expected_character", "variant", "run_id", "old_score",
        "raw_component_score", "new_stability_score", "degenerate",
        "degenerate_reason", "rankable", "window_character_fit", "rank",
    ]
    # rank per window among rankable rows (by raw_component_score desc, tie by name)
    by_window: dict[str, list[dict[str, Any]]] = {}
    for r in reeval_rows:
        by_window.setdefault(r["window"], []).append(r)
    for win, rs_rows in by_window.items():
        ordered = sorted(
            rs_rows,
            key=lambda r: (not r["rankable"], -(r["raw_component_score"] or -1e9), r["variant"]),
        )
        for i, r in enumerate(ordered):
            r["rank"] = i + 1 if r["rankable"] else None
    with reeval_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.DictWriter(fh, fieldnames=fields)
        wtr.writeheader()
        for r in sorted(reeval_rows, key=lambda x: (x["window"], x["variant"])):
            wtr.writerow({k: r.get(k) for k in fields})

    summary = {
        "metric_version": METRIC_VERSION,
        "score_version": SCORE_VERSION,
        "windows": {
            win: {
                "run_id": a["run_id"],
                "bars": a["bars"],
                "distinct_raw_states": sorted(a["distribution"].keys()),
                "bucket_parity": parity[win],
            }
            for win, a in audits.items()
        },
        "distinct_raw_states_all": sorted({s for a in audits.values() for s in a["distribution"]}),
        "canonical_bucket_map": {s: {"bucket": b, "reason": r} for s, (b, r) in STATE_BUCKET_MAP.items()},
        "artifacts": {
            "raw_state_distribution": str(dist_path),
            "raw_state_duration": str(dur_path),
            "raw_state_transition_matrix": str(mat_path),
            "state_bucket_mapping": str(map_path),
            "score_component_breakdown": str(comp_path),
            "pilot_reevaluation": str(reeval_path),
        },
    }
    (RESULTS_DIR / "audit_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    summary["reeval_rows"] = reeval_rows
    summary["parity"] = parity
    return summary
