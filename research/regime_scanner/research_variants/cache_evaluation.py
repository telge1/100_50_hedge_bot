"""Evaluate a window-set entirely from cached timelines (no scanner runs).

This is the fast path (Phase 7/22/25): for each variant/window, reuse a covering
completed timeline, slice it, score with the current metric/score version and
persist to ``research_window_evaluations``. A missing timeline is reported, never
silently built (build happens only via the explicit research_cache CLI).
"""

from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any

from research.regime_scanner.research_runs.parameters import (
    apply_parameter_overrides,
    build_baseline_parameter_set,
    parameter_hash,
)
from research.regime_scanner.research_variants.scoring import METRIC_VERSION, SCORE_VERSION
from research.regime_scanner.research_variants.timeline_cache import (
    MySQLCacheStore,
    evaluate_window_from_timeline,
    evaluation_hash,
    find_covering_completed_timeline,
    log_cache,
)
from research.regime_scanner.research_variants.windows import iso_utc, window_hash

RESULTS_DIR = Path(__file__).resolve().parents[1] / "results_research_variants_multiwindow"


def _variant_param_hash(variant: Any, *, exchange: str, symbol: str, data_source: str) -> str:
    base = build_baseline_parameter_set(exchange=exchange, symbol=symbol, data_source=data_source)
    params = (
        base
        if not variant.parameter_overrides
        else apply_parameter_overrides(base, variant.parameter_overrides)
    )
    return parameter_hash(params)


def evaluate_window_set_from_cache(
    research_store: Any,
    variant_store: Any,
    *,
    variant_set: Any,
    window_set: Any,
    exchange: str = "bybit",
    symbol: str = "APTUSDT",
    data_source: str = "mysql",
    rescore_only: bool = False,
) -> dict[str, Any]:
    cache = MySQLCacheStore(variant_store)
    rows: list[dict[str, Any]] = []
    t_start = time.perf_counter()

    for vi, variant in enumerate(variant_set.variants, 1):
        ph = _variant_param_hash(variant, exchange=exchange, symbol=symbol, data_source=data_source)
        log_cache(f"variant {vi}/{len(variant_set.variants)} {variant.name} parameter_hash={ph[:12]}")
        for window in window_set.windows:
            whash = window_hash(window)
            start = iso_utc(window.start_time)
            end = iso_utc(window.end_time)
            log_cache(f"  lookup timeline for window={window.name}")
            cov = find_covering_completed_timeline(
                research_store,
                parameter_hash=ph,
                symbol=symbol,
                data_source=data_source,
                window_start=start,
                window_end=end,
            )
            if cov is None:
                log_cache(f"  no covering timeline for {variant.name}/{window.name} -> skipped (no scanner run)")
                rows.append(
                    {
                        "window": window.name,
                        "expected_character": window.expected_character,
                        "variant": variant.name,
                        "timeline_id": None,
                        "status": "missing_timeline",
                        "reused": False,
                        "score": None,
                        "degenerate": None,
                        "rankable": None,
                        "character_fit": None,
                    }
                )
                continue
            timeline_id = str(cov["run_id"])
            log_cache(f"  reusing timeline {timeline_id[:8]} (completed) for {variant.name}/{window.name}")
            ev = evaluate_window_from_timeline(
                research_store,
                timeline_run_id=timeline_id,
                window_start=start,
                window_end=end,
                expected_character=window.expected_character,
            )
            eh = evaluation_hash(
                timeline_id=timeline_id,
                window_hash=whash,
                metric_version=METRIC_VERSION,
                score_version=SCORE_VERSION,
            )
            char_fit = ev.get("character_fit", {}).get("window_character_fit")
            cache.upsert_window_evaluation(
                timeline_id=timeline_id,
                window_hash=whash,
                window_name=window.name,
                metric_version=METRIC_VERSION,
                score_version=SCORE_VERSION,
                metrics=ev["metrics"],
                score=ev["stability_score"],
                degenerate=ev["degenerate"],
                degenerate_reason=ev["degenerate_reason"],
                rankable=ev["rankable"],
                character_fit=char_fit,
                evaluation_hash=eh,
            )
            rows.append(
                {
                    "window": window.name,
                    "expected_character": window.expected_character,
                    "variant": variant.name,
                    "timeline_id": timeline_id,
                    "status": "evaluated",
                    "reused": True,
                    "raw_component_score": ev["raw_component_score"],
                    "score": ev["stability_score"],
                    "degenerate": ev["degenerate"],
                    "degenerate_reason": ev["degenerate_reason"],
                    "rankable": ev["rankable"],
                    "character_fit": char_fit,
                    "sliced_trend_count": ev["sliced_trend_count"],
                    "sliced_structure_count": ev["sliced_structure_count"],
                    "evaluation_hash": eh,
                }
            )

    elapsed = time.perf_counter() - t_start

    # per-window rank among rankable
    by_window: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_window.setdefault(r["window"], []).append(r)
    for wname, wrows in by_window.items():
        rankable = [r for r in wrows if r.get("rankable")]
        ordered = sorted(rankable, key=lambda r: (-(r.get("raw_component_score") or -1e9), r["variant"]))
        for i, r in enumerate(ordered):
            r["rank"] = i + 1
        for r in wrows:
            r.setdefault("rank", None)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = RESULTS_DIR / f"{variant_set.name}_from_cache_evaluation.csv"
    fields = [
        "window", "expected_character", "variant", "timeline_id", "status", "reused",
        "raw_component_score", "score", "degenerate", "degenerate_reason", "rankable",
        "character_fit", "rank", "sliced_trend_count", "sliced_structure_count",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        wtr = csv.DictWriter(fh, fieldnames=fields)
        wtr.writeheader()
        for r in sorted(rows, key=lambda x: (x["window"], x["variant"])):
            wtr.writerow({k: r.get(k) for k in fields})

    summary = {
        "variant_set": variant_set.name,
        "window_set": window_set.name,
        "metric_version": METRIC_VERSION,
        "score_version": SCORE_VERSION,
        "rescore_only": rescore_only,
        "scanner_runs_started": 0,
        "evaluations": len([r for r in rows if r["status"] == "evaluated"]),
        "missing_timelines": len([r for r in rows if r["status"] == "missing_timeline"]),
        "elapsed_seconds": round(elapsed, 3),
        "rows": rows,
        "artifacts": {"from_cache_evaluation_csv": str(csv_path)},
    }
    (RESULTS_DIR / f"{variant_set.name}_from_cache_summary.json").write_text(
        json.dumps(summary, indent=2, default=str), encoding="utf-8"
    )
    return summary
