"""Phase C reporting, aggregation, and selected trace exports."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable, Sequence

from .event_finder import CrashEvent, EventFinderResult, drop_bucket
from .phase_b_runner import TRACE_FIELDS, write_phase_b_outputs
from .phase_c_runner import (
    DEFAULT_PHASE_C_OUTPUT_DIR,
    MODE_BASELINE,
    MODE_FULL_LOCK,
    MODE_ORACLE,
    run_phase_c,
)
from .config import EmergencyLockRecoveryConfig

PER_EVENT_FIELDS = (
    "event_id",
    "mode",
    "selection_type",
    "drop_bucket",
    "peak_timestamp",
    "peak_index",
    "peak_price",
    "low_timestamp",
    "low_index",
    "low_price",
    "max_drop_pct",
    "qualified_10_pct",
    "qualified_12_5_pct",
    "qualified_15_pct",
    "entry_timestamp",
    "entry_price",
    "simulation_start_index",
    "simulation_end_index",
    "lock_triggered",
    "lock_timestamp",
    "lock_price",
    "basket_pnl_at_lock",
    "short_avg_after_lock",
    "unlock_count",
    "relock_count",
    "failed_unlocks",
    "completed_unlock_stages",
    "minimum_basket_pnl_after_lock",
    "max_added_loss_after_lock",
    "loss_added_by_unlocks",
    "maximum_net_long_fraction",
    "break_even_reached",
    "break_even_timestamp",
    "bars_lock_to_break_even",
    "final_status",
    "final_net_pnl",
    "total_fees",
    "slippage_cost_usdt",
    "window_truncated_at_data_end",
    "incremental_final_pnl_vs_full_lock",
    "incremental_worst_loss_vs_full_lock",
    "baseline_better_than_full_lock",
    "baseline_worse_than_full_lock",
    "oracle_break_even_possible",
    "oracle_best_final_net_pnl",
    "oracle_earliest_break_even_timestamp",
    "oracle_required_net_long_fraction",
    "oracle_best_unlock_timestamp",
    "oracle_bound_type",
)


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fieldnames})


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(ordered[lo])
    w = idx - lo
    return float(ordered[lo] * (1.0 - w) + ordered[hi] * w)


def _safe_median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _safe_mean(values: list[float]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def aggregate_rows(
    rows: Sequence[dict[str, Any]],
    *,
    mode: str | None = None,
    bucket: str | None = None,
) -> dict[str, Any]:
    filtered = [
        r
        for r in rows
        if (mode is None or r.get("mode") == mode)
        and (bucket is None or r.get("drop_bucket") == bucket)
    ]
    n = len(filtered)
    if n == 0:
        return {
            "mode": mode,
            "drop_bucket": bucket or "all",
            "event_count": 0,
        }

    triggered = [r for r in filtered if r.get("lock_triggered")]
    finals = [float(r["final_net_pnl"]) for r in filtered if r.get("final_net_pnl") is not None]
    added = [
        float(r["max_added_loss_after_lock"])
        for r in filtered
        if r.get("max_added_loss_after_lock") is not None
    ]
    unlocks = [float(r.get("unlock_count") or 0) for r in filtered]
    relocks = [float(r.get("relock_count") or 0) for r in filtered]
    be_bars = [
        float(r["bars_lock_to_break_even"])
        for r in filtered
        if r.get("break_even_reached") and r.get("bars_lock_to_break_even") is not None
    ]
    better = sum(1 for r in filtered if r.get("baseline_better_than_full_lock") is True)
    worse = sum(1 for r in filtered if r.get("baseline_worse_than_full_lock") is True)

    oracle_possible = [
        r for r in filtered if r.get("mode") == MODE_ORACLE and r.get("oracle_break_even_possible")
    ]
    # Capture rate needs joining baseline+oracle by event — handled at caller for oracle mode.
    return {
        "mode": mode,
        "drop_bucket": bucket or "all",
        "event_count": n,
        "emergency_trigger_rate": (len(triggered) / n) if n else None,
        "break_even_rate": (
            sum(1 for r in filtered if r.get("break_even_reached")) / n if n else None
        ),
        "open_at_end_rate": (
            sum(1 for r in filtered if r.get("final_status") == "OPEN_AT_DATA_END") / n
            if n
            else None
        ),
        "timeout_rate": (
            sum(1 for r in filtered if r.get("final_status") == "STOPPED_TIMEOUT") / n
            if n
            else None
        ),
        "no_trigger_rate": (
            sum(1 for r in filtered if r.get("final_status") == "NO_EMERGENCY_TRIGGER") / n
            if n
            else None
        ),
        "median_final_net_pnl": _safe_median(finals),
        "mean_final_net_pnl": _safe_mean(finals),
        "worst_final_net_pnl": min(finals) if finals else None,
        "median_max_added_loss": _safe_median(added),
        "p90_max_added_loss": _percentile(added, 0.90),
        "p95_max_added_loss": _percentile(added, 0.95),
        "worst_max_added_loss": max(added) if added else None,
        "median_unlock_count": _safe_median(unlocks),
        "median_relock_count": _safe_median(relocks),
        "failed_unlock_rate": (
            sum(1 for r in filtered if int(r.get("failed_unlocks") or 0) > 0) / n if n else None
        ),
        "median_bars_to_break_even": _safe_median(be_bars),
        "p90_bars_to_break_even": _percentile(be_bars, 0.90),
        "baseline_better_than_full_lock_rate": (better / n) if mode == MODE_BASELINE else None,
        "baseline_worse_than_full_lock_rate": (worse / n) if mode == MODE_BASELINE else None,
        "oracle_break_even_possible_rate": (
            (len(oracle_possible) / n) if mode == MODE_ORACLE else None
        ),
        "window_truncated_rate": (
            sum(1 for r in filtered if r.get("window_truncated_at_data_end")) / n if n else None
        ),
    }


def compute_capture_rate(rows: Sequence[dict[str, Any]]) -> float | None:
    by_event: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        by_event.setdefault(str(r["event_id"]), {})[str(r["mode"])] = r
    eligible = 0
    captured = 0
    for modes in by_event.values():
        oracle = modes.get(MODE_ORACLE)
        baseline = modes.get(MODE_BASELINE)
        if not oracle or not baseline:
            continue
        if not oracle.get("oracle_break_even_possible"):
            continue
        eligible += 1
        if baseline.get("break_even_reached"):
            captured += 1
    if eligible == 0:
        return None
    return captured / eligible


def select_trace_event_ids(rows: Sequence[dict[str, Any]], *, k: int = 3) -> dict[str, list[str]]:
    baseline = [r for r in rows if r.get("mode") == MODE_BASELINE and r.get("lock_triggered")]
    oracle = [r for r in rows if r.get("mode") == MODE_ORACLE]

    def _ids(items: list[dict[str, Any]], key, reverse: bool) -> list[str]:
        ordered = sorted(items, key=key, reverse=reverse)
        out: list[str] = []
        for r in ordered:
            eid = str(r["event_id"])
            if eid not in out:
                out.append(eid)
            if len(out) >= k:
                break
        return out

    best_baseline = _ids(
        [r for r in baseline if r.get("final_net_pnl") is not None],
        key=lambda r: float(r["final_net_pnl"]),
        reverse=True,
    )
    worst_loss = _ids(
        [r for r in baseline if r.get("max_added_loss_after_lock") is not None],
        key=lambda r: float(r["max_added_loss_after_lock"]),
        reverse=True,
    )
    most_relocks = _ids(
        baseline,
        key=lambda r: (int(r.get("relock_count") or 0), int(r.get("unlock_count") or 0)),
        reverse=True,
    )
    oracle_fail: list[str] = []
    by_event: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        by_event.setdefault(str(r["event_id"]), {})[str(r["mode"])] = r
    candidates = []
    for eid, modes in by_event.items():
        o = modes.get(MODE_ORACLE)
        b = modes.get(MODE_BASELINE)
        if o and b and o.get("oracle_break_even_possible") and not b.get("break_even_reached"):
            candidates.append(b)
    oracle_fail = _ids(
        candidates,
        key=lambda r: float(r.get("max_added_loss_after_lock") or 0.0),
        reverse=True,
    )
    return {
        "best_baseline_events": best_baseline,
        "worst_added_loss_events": worst_loss,
        "most_relocks_events": most_relocks,
        "oracle_possible_but_baseline_failed": oracle_fail,
    }


def write_phase_c_outputs(
    payload: dict[str, Any],
    output_dir: str | Path = DEFAULT_PHASE_C_OUTPUT_DIR,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    finder: EventFinderResult = payload["finder"]
    rows: list[dict[str, Any]] = payload["per_event_rows"]

    raw_path = out / "raw_event_candidates.csv"
    _write_csv(
        raw_path,
        [c.as_dict() for c in finder.raw_candidates],
        fieldnames=list(finder.raw_candidates[0].as_dict().keys())
        if finder.raw_candidates
        else ["candidate_id"],
    )
    manifest_path = out / "event_manifest.csv"
    _write_csv(
        manifest_path,
        [e.as_dict() for e in finder.events],
        fieldnames=list(finder.events[0].as_dict().keys())
        if finder.events
        else ["event_id"],
    )
    dedupe_path = out / "dedupe_manifest.csv"
    dedupe_fields = sorted({k for row in finder.dedupe_rows for k in row.keys()}) or [
        "candidate_id"
    ]
    _write_csv(dedupe_path, finder.dedupe_rows, dedupe_fields)

    per_event_path = out / "per_event_mode_summary.csv"
    _write_csv(per_event_path, rows, PER_EVENT_FIELDS)

    baseline_rows = [r for r in rows if r["mode"] == MODE_BASELINE]
    full_rows = [r for r in rows if r["mode"] == MODE_FULL_LOCK]
    oracle_rows = [r for r in rows if r["mode"] == MODE_ORACLE]
    _write_csv(out / "baseline_per_event_summary.csv", baseline_rows, PER_EVENT_FIELDS)
    _write_csv(out / "full_lock_control_summary.csv", full_rows, PER_EVENT_FIELDS)
    _write_csv(out / "oracle_diagnostic_summary.csv", oracle_rows, PER_EVENT_FIELDS)

    capture = compute_capture_rate(rows)
    agg_mode_rows = []
    for mode in (MODE_BASELINE, MODE_FULL_LOCK, MODE_ORACLE):
        agg = aggregate_rows(rows, mode=mode)
        if mode == MODE_BASELINE:
            agg["baseline_capture_rate_when_oracle_possible"] = capture
        agg_mode_rows.append(agg)
    agg_mode_fields = sorted({k for r in agg_mode_rows for k in r.keys()})
    _write_csv(out / "aggregate_by_mode.csv", agg_mode_rows, agg_mode_fields)

    buckets = ("all", "10–12.5%", "12.5–15%", ">=15%")
    agg_bucket_rows = []
    for mode in (MODE_BASELINE, MODE_FULL_LOCK, MODE_ORACLE):
        for bucket in buckets:
            if bucket == "all":
                agg_bucket_rows.append(aggregate_rows(rows, mode=mode, bucket=None))
            else:
                agg_bucket_rows.append(aggregate_rows(rows, mode=mode, bucket=bucket))
    agg_bucket_fields = sorted({k for r in agg_bucket_rows for k in r.keys()})
    _write_csv(out / "aggregate_by_drop_bucket.csv", agg_bucket_rows, agg_bucket_fields)

    summary = {
        "raw_candidate_count": len(finder.raw_candidates),
        "deduped_event_count": len(finder.events),
        "qualified_10_pct_count": sum(1 for e in finder.events if e.qualified_10_pct),
        "qualified_12_5_pct_count": sum(1 for e in finder.events if e.qualified_12_5_pct),
        "qualified_15_pct_count": sum(1 for e in finder.events if e.qualified_15_pct),
        "baseline_capture_rate_when_oracle_possible": capture,
        "aggregates_by_mode": agg_mode_rows,
        "note": (
            "Oracle mode is NON_CAUSAL_ORACLE_DIAGNOSTIC and must not be treated "
            "as a tradable strategy. Event selection is hindsight_selected_stress_event."
        ),
    }
    summary_path = out / "aggregate_summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")

    # Selected traces
    selected = select_trace_event_ids(rows, k=3)
    traces_root = out / "traces"
    baseline_results = payload.get("baseline_results") or {}
    for folder, eids in selected.items():
        dest = traces_root / folder
        dest.mkdir(parents=True, exist_ok=True)
        for eid in eids:
            result = baseline_results.get(eid)
            if result is None:
                continue
            write_phase_b_outputs(result, dest / eid)

    with (out / "selected_trace_events.json").open("w", encoding="utf-8") as handle:
        json.dump(selected, handle, indent=2, sort_keys=True)
        handle.write("\n")

    return {
        "raw_event_candidates_csv": raw_path,
        "event_manifest_csv": manifest_path,
        "dedupe_manifest_csv": dedupe_path,
        "per_event_mode_summary_csv": per_event_path,
        "aggregate_summary_json": summary_path,
        "output_dir": out,
    }


def run_phase_c_to_disk(
    cfg: EmergencyLockRecoveryConfig | None = None,
    candles: Sequence[dict[str, Any]] | None = None,
    output_dir: str | Path = DEFAULT_PHASE_C_OUTPUT_DIR,
) -> dict[str, Any]:
    payload = run_phase_c(cfg=cfg, candles=candles)
    paths = write_phase_c_outputs(payload, output_dir=output_dir)
    payload["output_paths"] = {k: str(v) for k, v in paths.items()}
    return payload
