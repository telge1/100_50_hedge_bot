"""Phase D aggregation, candidate gates, and selected trace export."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any, Sequence

from .config import EmergencyLockRecoveryConfig
from .phase_d_runner import (
    DEFAULT_PHASE_D_OUTPUT_DIR,
    MAIN_RELOCK_VARIANT,
    run_phase_d,
)

PER_EVENT_FIELDS = (
    "event_id",
    "signal_name",
    "relock_variant",
    "drop_bucket",
    "max_drop_pct",
    "peak_timestamp",
    "lock_timestamp",
    "basket_pnl_at_lock",
    "short_avg_after_lock",
    "signal_count",
    "unlock_count",
    "unlock_attempt_count",
    "relock_count",
    "failed_unlocks",
    "completed_unlock_stages",
    "minimum_basket_pnl_after_lock",
    "max_added_loss_after_lock",
    "maximum_net_long_fraction",
    "break_even_reached",
    "break_even_timestamp",
    "bars_lock_to_break_even",
    "final_status",
    "final_net_pnl",
    "incremental_final_pnl_vs_full_lock",
    "incremental_worst_pnl_vs_full_lock",
    "better_than_full_lock",
    "worse_than_full_lock",
    "oracle_break_even_possible",
    "oracle_captured",
    "total_fees",
    "slippage_cost_usdt",
    "window_truncated_at_data_end",
)


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fields})


def _median(xs: list[float]) -> float | None:
    return float(statistics.median(xs)) if xs else None


def _mean(xs: list[float]) -> float | None:
    return float(statistics.fmean(xs)) if xs else None


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ordered = sorted(xs)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * p
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(ordered[lo])
    w = idx - lo
    return float(ordered[lo] * (1 - w) + ordered[hi] * w)


def aggregate_signal(
    rows: Sequence[dict[str, Any]],
    *,
    signal_name: str,
    relock_variant: str,
    drop_bucket: str | None = None,
) -> dict[str, Any]:
    filtered = [
        r
        for r in rows
        if r.get("signal_name") == signal_name
        and r.get("relock_variant") == relock_variant
        and (drop_bucket is None or r.get("drop_bucket") == drop_bucket)
    ]
    n = len(filtered)
    if n == 0:
        return {
            "signal_name": signal_name,
            "relock_variant": relock_variant,
            "drop_bucket": drop_bucket or "all",
            "event_count": 0,
        }

    incr = [
        float(r["incremental_final_pnl_vs_full_lock"])
        for r in filtered
        if r.get("incremental_final_pnl_vs_full_lock") is not None
    ]
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
    better = sum(1 for r in filtered if r.get("better_than_full_lock") is True)
    worse = sum(1 for r in filtered if r.get("worse_than_full_lock") is True)
    equal = n - better - worse
    be = sum(1 for r in filtered if r.get("break_even_reached") is True)
    oracle_possible = sum(1 for r in filtered if r.get("oracle_break_even_possible") is True)
    oracle_cap = sum(1 for r in filtered if r.get("oracle_captured") is True)
    failed_ev = sum(1 for r in filtered if int(r.get("failed_unlocks") or 0) > 0)
    fees = [float(r["total_fees"]) for r in filtered if r.get("total_fees") is not None]
    frozen = [
        abs(float(r["basket_pnl_at_lock"]))
        for r in filtered
        if r.get("basket_pnl_at_lock") is not None
    ]

    return {
        "signal_name": signal_name,
        "relock_variant": relock_variant,
        "drop_bucket": drop_bucket or "all",
        "event_count": n,
        "break_even_count": be,
        "break_even_rate": be / n,
        "better_than_full_lock_count": better,
        "better_than_full_lock_rate": better / n,
        "worse_than_full_lock_count": worse,
        "worse_than_full_lock_rate": worse / n,
        "equal_to_full_lock_rate": equal / n,
        "median_incremental_final_pnl_vs_full_lock": _median(incr),
        "mean_incremental_final_pnl_vs_full_lock": _mean(incr),
        "worst_incremental_final_pnl_vs_full_lock": min(incr) if incr else None,
        "median_max_added_loss": _median(added),
        "p90_max_added_loss": _pct(added, 0.90),
        "p95_max_added_loss": _pct(added, 0.95),
        "worst_max_added_loss": max(added) if added else None,
        "median_unlock_count": _median(unlocks),
        "median_relock_count": _median(relocks),
        "failed_unlock_event_rate": failed_ev / n,
        "oracle_possible_event_count": oracle_possible,
        "oracle_capture_count": oracle_cap,
        "oracle_capture_rate": (oracle_cap / oracle_possible) if oracle_possible else None,
        "median_bars_to_break_even": _median(be_bars),
        "total_fees": sum(fees) if fees else 0.0,
        "median_abs_basket_pnl_at_lock": _median(frozen),
    }


def decide_phase_e_candidates(
    aggregates: Sequence[dict[str, Any]],
    *,
    rebound_key: tuple[str, str] = ("rebound_baseline", MAIN_RELOCK_VARIANT),
) -> list[dict[str, Any]]:
    by_key = {(r["signal_name"], r["relock_variant"]): r for r in aggregates if r.get("drop_bucket") == "all"}
    rebound = by_key.get(rebound_key)
    decisions: list[dict[str, Any]] = []
    for key, agg in sorted(by_key.items()):
        signal_name, relock_variant = key
        if signal_name == "full_lock_control":
            continue
        # Main ranking uses common_pct only.
        if relock_variant != MAIN_RELOCK_VARIANT:
            decisions.append(
                {
                    "signal_name": signal_name,
                    "relock_variant": relock_variant,
                    "passes_better_vs_worse": False,
                    "passes_median_incremental_pnl": False,
                    "passes_median_added_loss": False,
                    "passes_p90_added_loss": False,
                    "passes_failed_unlock_rate": False,
                    "passes_oracle_capture": False,
                    "phase_e_candidate": False,
                    "rejection_reasons": "diagnostic_relock_variant_excluded_from_main_ranking",
                    "warning_worst_loss_gt_frozen": None,
                }
            )
            continue

        reasons: list[str] = []
        p1 = (agg.get("better_than_full_lock_rate") or 0) > (
            agg.get("worse_than_full_lock_rate") or 0
        )
        if not p1:
            reasons.append("better_than_full_lock_rate_not_gt_worse")
        p2 = (agg.get("median_incremental_final_pnl_vs_full_lock") or -1e18) > 0
        if not p2:
            reasons.append("median_incremental_final_pnl_not_gt_0")

        p3 = p4 = p5 = True
        if rebound is not None:
            p3 = (agg.get("median_max_added_loss") or 1e18) < (
                rebound.get("median_max_added_loss") or 0
            )
            if not p3:
                reasons.append("median_max_added_loss_not_lt_rebound")
            p4 = (agg.get("p90_max_added_loss") or 1e18) < (
                rebound.get("p90_max_added_loss") or 0
            )
            if not p4:
                reasons.append("p90_max_added_loss_not_lt_rebound")
            p5 = (agg.get("failed_unlock_event_rate") or 1e18) < (
                rebound.get("failed_unlock_event_rate") or 0
            )
            if not p5:
                reasons.append("failed_unlock_event_rate_not_lt_rebound")
        p6 = int(agg.get("oracle_capture_count") or 0) >= 2
        if not p6:
            reasons.append("oracle_capture_count_lt_2")

        passes = all([p1, p2, p3, p4, p5, p6])
        worst_added = agg.get("worst_max_added_loss")
        frozen = agg.get("median_abs_basket_pnl_at_lock")
        warn_worst = (
            worst_added is not None
            and frozen is not None
            and float(worst_added) > float(frozen)
        )
        decisions.append(
            {
                "signal_name": signal_name,
                "relock_variant": relock_variant,
                "passes_better_vs_worse": p1,
                "passes_median_incremental_pnl": p2,
                "passes_median_added_loss": p3,
                "passes_p90_added_loss": p4,
                "passes_failed_unlock_rate": p5,
                "passes_oracle_capture": p6,
                "phase_e_candidate": passes,
                "rejection_reasons": ";".join(reasons) if reasons else "",
                "warning_worst_loss_gt_frozen": warn_worst,
                "warning_single_event_driver": bool(
                    int(agg.get("break_even_count") or 0) <= 1
                    and (agg.get("better_than_full_lock_rate") or 0) > 0
                ),
            }
        )
    return decisions


def write_phase_d_outputs(
    payload: dict[str, Any],
    output_dir: str | Path = DEFAULT_PHASE_D_OUTPUT_DIR,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = payload["per_event_rows"]
    _write_csv(out / "signal_per_event_summary.csv", rows, PER_EVENT_FIELDS)

    # Aggregates
    keys = sorted({(r["signal_name"], r["relock_variant"]) for r in rows})
    agg_rows = []
    for signal_name, relock_variant in keys:
        agg_rows.append(
            aggregate_signal(rows, signal_name=signal_name, relock_variant=relock_variant)
        )
    agg_fields = sorted({k for r in agg_rows for k in r.keys()})
    _write_csv(out / "signal_aggregate_summary.csv", agg_rows, agg_fields)

    bucket_rows = []
    for signal_name, relock_variant in keys:
        for bucket in ("all", "10–12.5%", "12.5–15%", ">=15%"):
            if bucket == "all":
                bucket_rows.append(
                    aggregate_signal(
                        rows, signal_name=signal_name, relock_variant=relock_variant
                    )
                )
            else:
                bucket_rows.append(
                    aggregate_signal(
                        rows,
                        signal_name=signal_name,
                        relock_variant=relock_variant,
                        drop_bucket=bucket,
                    )
                )
    bucket_fields = sorted({k for r in bucket_rows for k in r.keys()})
    _write_csv(out / "signal_by_drop_bucket.csv", bucket_rows, bucket_fields)

    action_fields = sorted({k for r in payload["actions"] for k in r.keys()}) or ["event_id"]
    _write_csv(out / "signal_actions.csv", payload["actions"], action_fields)
    # Persist only triggered decisions in the main CSV to keep size bounded;
    # selected traces still retain denser diagnostics for inspected events.
    diag_rows = [d for d in payload["diagnostics"] if d.get("triggered")]
    diag_fields = sorted({k for r in diag_rows for k in r.keys()}) or [
        "event_id",
        "signal_name",
        "triggered",
        "reason",
    ]
    _write_csv(out / "signal_diagnostics.csv", diag_rows, diag_fields)

    decisions = decide_phase_e_candidates(agg_rows)
    dec_fields = sorted({k for r in decisions for k in r.keys()}) or ["signal_name"]
    _write_csv(out / "candidate_decision.csv", decisions, dec_fields)

    summary = {
        "event_count": len(payload["events"]),
        "protected_structure_adapter_available": payload.get(
            "protected_structure_adapter_available"
        ),
        "protected_structure_adapter_skip_reason": payload.get(
            "protected_structure_adapter_skip_reason"
        ),
        "phase_e_candidates": [
            d for d in decisions if d.get("phase_e_candidate")
        ],
        "aggregates_main_common_pct": [
            r for r in agg_rows if r.get("relock_variant") == MAIN_RELOCK_VARIANT
        ],
        "note": (
            "Main ranking uses relock_variant=common_pct only. "
            "signal_invalidation rows are diagnostic."
        ),
    }
    summary_path = out / "phase_d_summary.json"
    with summary_path.open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
        handle.write("\n")

    # Selected traces from rebound + best candidate-like signals (common_pct)
    _export_selected_traces(payload, out / "traces")
    return {
        "signal_per_event_summary_csv": out / "signal_per_event_summary.csv",
        "signal_aggregate_summary_csv": out / "signal_aggregate_summary.csv",
        "candidate_decision_csv": out / "candidate_decision.csv",
        "phase_d_summary_json": summary_path,
        "output_dir": out,
    }


def _export_selected_traces(payload: dict[str, Any], traces_root: Path) -> None:
    rows = [
        r
        for r in payload["per_event_rows"]
        if r.get("relock_variant") == MAIN_RELOCK_VARIANT
        and r.get("signal_name") != "full_lock_control"
    ]
    results = payload["results"]

    def pick(items: list[dict[str, Any]], key, reverse: bool, k: int = 2) -> list[tuple[str, str]]:
        ordered = sorted(items, key=key, reverse=reverse)
        out: list[tuple[str, str]] = []
        for r in ordered:
            pair = (str(r["event_id"]), str(r["signal_name"]))
            if pair not in out:
                out.append(pair)
            if len(out) >= k:
                break
        return out

    selections = {
        "best_vs_full_lock": pick(
            [r for r in rows if r.get("incremental_final_pnl_vs_full_lock") is not None],
            key=lambda r: float(r["incremental_final_pnl_vs_full_lock"]),
            reverse=True,
        ),
        "worst_added_loss": pick(
            [r for r in rows if r.get("max_added_loss_after_lock") is not None],
            key=lambda r: float(r["max_added_loss_after_lock"]),
            reverse=True,
        ),
        "oracle_possible_but_failed": pick(
            [
                r
                for r in rows
                if r.get("oracle_break_even_possible") and not r.get("break_even_reached")
            ],
            key=lambda r: float(r.get("max_added_loss_after_lock") or 0),
            reverse=True,
        ),
        "successful_break_even": pick(
            [r for r in rows if r.get("break_even_reached")],
            key=lambda r: float(r.get("final_net_pnl") or 0),
            reverse=True,
        ),
        "false_unlock_examples": pick(
            [r for r in rows if int(r.get("failed_unlocks") or 0) > 0],
            key=lambda r: int(r.get("failed_unlocks") or 0),
            reverse=True,
        ),
    }
    for folder, pairs in selections.items():
        dest = traces_root / folder
        dest.mkdir(parents=True, exist_ok=True)
        for event_id, signal_name in pairs:
            key = (event_id, signal_name, MAIN_RELOCK_VARIANT)
            result = results.get(key)
            if result is None:
                continue
            # Adapt to write_phase_b_outputs shape
            adapted = {
                "summary": result["summary"],
                "trace": [],
                "actions": result["actions"],
            }
            # Write actions + summary only
            sub = dest / f"{event_id}__{signal_name}"
            sub.mkdir(parents=True, exist_ok=True)
            with (sub / "summary.json").open("w", encoding="utf-8") as handle:
                json.dump(result["summary"], handle, indent=2, sort_keys=True)
                handle.write("\n")
            if result["actions"]:
                fields = sorted({k for a in result["actions"] for k in a.keys()})
                _write_csv(sub / "actions.csv", result["actions"], fields)
            if result["diagnostics"]:
                fields = sorted({k for a in result["diagnostics"] for k in a.keys()})
                _write_csv(sub / "diagnostics.csv", result["diagnostics"], fields)

    with (traces_root / "selected.json").open("w", encoding="utf-8") as handle:
        json.dump(selections, handle, indent=2, sort_keys=True)
        handle.write("\n")


def run_phase_d_to_disk(
    *,
    output_dir: str | Path = DEFAULT_PHASE_D_OUTPUT_DIR,
    cfg: EmergencyLockRecoveryConfig | None = None,
) -> dict[str, Any]:
    payload = run_phase_d(cfg=cfg)
    paths = write_phase_d_outputs(payload, output_dir=output_dir)
    payload["output_paths"] = {k: str(v) for k, v in paths.items()}
    return payload
