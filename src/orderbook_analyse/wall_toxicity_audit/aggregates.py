"""Group and baseline aggregations for wall toxicity batch outcomes."""

from __future__ import annotations

import statistics
from collections import defaultdict
from typing import Any, Iterable, Sequence

from orderbook_analyse.wall_toxicity_audit.types import OutcomeParams, score_bin


def _median(vals: list[float]) -> float | None:
    return None if not vals else float(statistics.median(vals))


def _mean(vals: list[float]) -> float | None:
    return None if not vals else float(statistics.fmean(vals))


def _rate(flags: list[bool | None]) -> float | None:
    known = [f for f in flags if f is not None]
    if not known:
        return None
    return sum(1 for f in known if f) / len(known)


def summarize_outcome_group(
    rows: Sequence[dict[str, Any]],
    *,
    group_name: str,
    group_value: str,
    horizon_seconds: int,
    params: OutcomeParams,
) -> dict[str, Any]:
    subset = [r for r in rows if int(r.get("horizon_seconds") or 0) == horizon_seconds]
    eligible = [r for r in subset if r.get("outcome_eligible")]
    complete = [r for r in eligible if r.get("forward_data_complete")]
    n = len(eligible)
    uncertain = n < params.uncertain_sample_n

    def col(name: str) -> list[float]:
        out: list[float] = []
        for r in complete:
            v = r.get(name)
            if v is not None and v != "":
                out.append(float(v))
        return out

    held = [r.get("held") for r in complete]
    broken = [r.get("broken") for r in complete]
    acceptance = [r.get("acceptance") for r in complete]
    failed = [r.get("failed_break") for r in complete]

    return {
        "group_name": group_name,
        "group_value": group_value,
        "horizon_seconds": horizon_seconds,
        "n": n,
        "n_complete_forward": len(complete),
        "complete_forward_share": None if n == 0 else len(complete) / n,
        "median_forward_return_bps": _median(col("forward_return_bps")),
        "mean_forward_return_bps": _mean(col("forward_return_bps")),
        "median_mfe_up_bps": _median(col("mfe_up_bps")),
        "median_mae_down_bps": _median(col("mae_down_bps")),
        "hold_rate": _rate(held),
        "break_rate": _rate(broken),
        "acceptance_rate": _rate(acceptance),
        "failed_break_rate": _rate(failed),
        "median_time_to_touch_seconds": _median(col("time_to_first_touch_seconds")),
        "median_time_to_break_seconds": _median(col("time_to_break_seconds")),
        "sample_uncertain": uncertain,
    }


def assign_detail_groups(
    detail: dict[str, Any], *, params: OutcomeParams
) -> dict[str, str]:
    rel = score_bin(
        _f(detail.get("reliability_score")),
        low_max=params.score_low_max,
        medium_max=params.score_medium_max,
    )
    tox = score_bin(
        _f(detail.get("toxicity_score")),
        low_max=params.score_low_max,
        medium_max=params.score_medium_max,
    )
    age = _f(detail.get("age_seconds")) or 0.0
    if age <= params.short_life_seconds:
        life = "SHORT"
    elif age >= params.long_life_seconds:
        life = "LONG"
    else:
        life = "MEDIUM"
    rwtr = _f(detail.get("removed_without_trade_ratio"))
    if rwtr is None:
        rwtr_bin = "UNKNOWN"
    elif rwtr >= params.high_removed_without_trade_ratio:
        rwtr_bin = "HIGH"
    else:
        rwtr_bin = "LOW"
    near = bool(detail.get("is_near_market"))
    touched = bool(detail.get("bucket_touched") or detail.get("was_tested"))
    executed = bool(detail.get("trades_in_bucket")) or (
        (_f(detail.get("removed_without_trade_ratio")) or 1.0) < 0.5
        and (_f(detail.get("trade_qty_in_bucket")) or 0) > 0
    )
    migrated = int(detail.get("migration_event_count") or 0) > 0
    return {
        "classification": str(detail.get("classification") or ""),
        "side": str(detail.get("side") or ""),
        "near_remote": "NEAR" if near else "REMOTE",
        "touched_untouched": "TOUCHED" if touched else "UNTOUCHED",
        "executed_bin": "EXECUTED" if executed else "NOT_EXECUTED",
        "migrated_bin": "MIGRATED" if migrated else "NOT_MIGRATED",
        "toxicity_bin": tox,
        "reliability_bin": rel,
        "spoofing_suspicion": str(detail.get("spoofing_suspicion") or ""),
        "lifetime_bin": life,
        "removed_without_trade_bin": rwtr_bin,
    }


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


def baseline_membership(
    detail: dict[str, Any], *, params: OutcomeParams
) -> dict[str, bool]:
    near = bool(detail.get("is_near_market"))
    touched = bool(detail.get("bucket_touched") or detail.get("was_tested"))
    rel = _f(detail.get("reliability_score")) or 0.0
    tox = _f(detail.get("toxicity_score")) or 0.0
    sus = str(detail.get("spoofing_suspicion") or "")
    reliable = rel >= params.reliable_min_score
    toxic_excl = sus != "HIGH" and tox <= params.toxic_exclude_max_score
    return {
        "ALL_WALLS": True,
        "NEAR_MARKET": near,
        "RELIABLE": reliable,
        "TOXIC_EXCLUDED": toxic_excl,
        "TOUCHED": touched,
        "TOUCHED_AND_RELIABLE": touched and reliable,
    }


def aggregate_groups(
    outcome_rows: Sequence[dict[str, Any]],
    details: Sequence[dict[str, Any]],
    *,
    params: OutcomeParams,
    reference_point: str = "FROM_CLASSIFICATION",
) -> list[dict[str, Any]]:
    detail_by_id = {d["wall_sequence_id"]: d for d in details}
    # Enrich outcomes with group keys
    enriched: list[dict[str, Any]] = []
    for r in outcome_rows:
        if r.get("reference_point") != reference_point:
            continue
        d = detail_by_id.get(r["wall_sequence_id"])
        if not d:
            continue
        groups = assign_detail_groups(d, params=params)
        row = dict(r)
        row.update(groups)
        row["outcome_eligible"] = bool(d.get("outcome_eligible", True))
        enriched.append(row)

    group_keys = [
        "classification",
        "side",
        "near_remote",
        "touched_untouched",
        "executed_bin",
        "migrated_bin",
        "toxicity_bin",
        "reliability_bin",
        "spoofing_suspicion",
        "lifetime_bin",
        "removed_without_trade_bin",
    ]
    out: list[dict[str, Any]] = []
    for horizon in params.forward_seconds:
        for gk in group_keys:
            buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in enriched:
                if int(r["horizon_seconds"]) != horizon:
                    continue
                buckets[str(r.get(gk) or "")].append(r)
            for val, rows in sorted(buckets.items()):
                s = summarize_outcome_group(
                    rows, group_name=gk, group_value=val, horizon_seconds=horizon, params=params
                )
                s["reference_point"] = reference_point
                out.append(s)
    return out


def aggregate_baselines(
    outcome_rows: Sequence[dict[str, Any]],
    details: Sequence[dict[str, Any]],
    *,
    params: OutcomeParams,
    reference_point: str = "FROM_CLASSIFICATION",
) -> list[dict[str, Any]]:
    detail_by_id = {d["wall_sequence_id"]: d for d in details}
    out: list[dict[str, Any]] = []
    for horizon in params.forward_seconds:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in outcome_rows:
            if r.get("reference_point") != reference_point:
                continue
            if int(r["horizon_seconds"]) != horizon:
                continue
            d = detail_by_id.get(r["wall_sequence_id"])
            if not d:
                continue
            row = dict(r)
            row["outcome_eligible"] = bool(d.get("outcome_eligible", True))
            membership = baseline_membership(d, params=params)
            for name, ok in membership.items():
                if ok:
                    buckets[name].append(row)
        for name, rows in sorted(buckets.items()):
            s = summarize_outcome_group(
                rows,
                group_name="baseline",
                group_value=name,
                horizon_seconds=horizon,
                params=params,
            )
            s["reference_point"] = reference_point
            out.append(s)
    return out
