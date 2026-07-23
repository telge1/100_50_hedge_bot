"""Diagnostics / aggregation helpers for adaptive distance staging validation."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any, Sequence

from research.backtests.adaptive_distance_staging import (
    classify_distance_status,
    compute_original_distance_pct,
    is_adaptive_profile,
    select_distance_bucket,
    summarize_bucket_key,
    theoretical_bucket_label,
)
from research.backtests.inventory_mtm_freeze import safe_float
from research.backtests.two_early_medium_multistart_metrics import compare_pair, summarize_pairs


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            return [value]
    return [value]


def _finite_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _collect_plan_distances(plans: Sequence[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    for row in plans:
        d = _finite_float(row.get("original_distance_pct"))
        if d is None:
            first = _finite_float(row.get("first_leg_fill_price"))
            full = _finite_float(row.get("full_trigger_price"))
            if first is not None and full is not None:
                d = compute_original_distance_pct(first, full)
        if d is not None and d > 0:
            out.append(float(d))
    return out


def extract_adaptive_diagnostics(result: Any) -> dict[str, Any]:
    """Pull frozen adaptive / TEM diagnostic distance fields from result."""
    excerpt = dict(getattr(result, "final_strategy_state_excerpt", None) or {})
    plan = dict(excerpt.get("research_second_leg_price_staging_plan") or {})
    plans = [dict(r) for r in (excerpt.get("research_second_leg_price_staging_plans") or [])]

    chosen = plan if plan else {}
    if not chosen and plans:
        for row in reversed(plans):
            if row.get("accepted") or row.get("distance_bucket") is not None or row.get(
                "original_distance_pct"
            ) is not None or row.get("theoretical_distance_bucket") is not None:
                chosen = dict(row)
                break

    # Intent metadata fallback
    if chosen.get("original_distance_pct") is None and not chosen.get("theoretical_distance_bucket"):
        for intent in getattr(result, "intent_log", None) or []:
            meta = dict(intent.get("metadata_excerpt") or {})
            if meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp"):
                for key in (
                    "original_distance_pct",
                    "distance_bucket",
                    "theoretical_distance_bucket",
                    "distance_status",
                    "grid_step_pct",
                    "requested_absolute_stage_distances_pct",
                    "effective_absolute_stage_distances_pct",
                    "requested_price_fractions",
                    "effective_price_fractions",
                    "requested_stage_count",
                    "capped_stage_count",
                    "stage_cap_applied",
                    "selected_stage_count",
                    "selected_price_fractions",
                    "selected_qty_fractions",
                    "requested_qty_fractions",
                    "effective_qty_fractions",
                    "effective_stage_count_after_rounding",
                    "skipped_small_stages",
                    "merged_stage_count",
                    "residual_qty",
                    "fallback_used",
                    "adaptive_family",
                    "fixed_step_qty_family",
                    "diagnostic_only",
                ):
                    if key in meta and chosen.get(key) is None:
                        chosen[key] = meta[key]
                break

    distances = _collect_plan_distances(plans)
    if not distances:
        d0 = _finite_float(chosen.get("original_distance_pct"))
        if d0 is not None and d0 > 0:
            distances = [d0]

    filled: list[int] = []
    planned_indices: list[int] = []
    intent_candles: list[int] = []
    for intent in getattr(result, "intent_log", None) or []:
        meta = dict(intent.get("metadata_excerpt") or {})
        if not (meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")):
            continue
        if meta.get("stage_index") is not None:
            planned_indices.append(int(meta["stage_index"]))
        if intent.get("candle_index") is not None:
            intent_candles.append(int(intent["candle_index"]))

    fill_candles: list[int] = []
    for fill in getattr(result, "fills_log", None) or []:
        meta = dict(fill.get("metadata_excerpt") or {})
        if not (meta.get("research_price_staging") or meta.get("is_staged_second_leg_tp")):
            continue
        if meta.get("stage_index") is not None:
            filled.append(int(meta["stage_index"]))
        if fill.get("candle_index") is not None:
            fill_candles.append(int(fill["candle_index"]))

    filled_u = sorted(set(filled))
    planned_u = sorted(set(planned_indices))
    unfilled = [i for i in planned_u if i not in set(filled_u)]

    first_delay = None
    if intent_candles and fill_candles:
        first_delay = min(fill_candles) - min(intent_candles)

    effective = chosen.get("effective_stage_count_after_rounding")
    if effective is None:
        effective = len(planned_u)

    distance_pct = _finite_float(chosen.get("original_distance_pct"))
    if distance_pct is None and distances:
        distance_pct = distances[-1]

    bucket_raw = chosen.get("distance_bucket")
    theoretical = chosen.get("theoretical_distance_bucket")
    if theoretical is None and distance_pct is not None:
        theoretical = theoretical_bucket_label(select_distance_bucket(distance_pct))
    if bucket_raw in (None, "") and theoretical and not chosen.get("diagnostic_only"):
        bucket_raw = theoretical

    status = chosen.get("distance_status")
    if not status:
        profile = None
        for intent in getattr(result, "intent_log", None) or []:
            meta = dict(intent.get("metadata_excerpt") or {})
            if meta.get("research_price_staging_profile"):
                profile = meta.get("research_price_staging_profile")
                break
        has_plan = bool(plans) or bool(chosen)
        status = classify_distance_status(
            profile=profile,
            max_cycle=4 if has_plan else None,
            distance_pct=distance_pct,
            bucket=bucket_raw or theoretical,
            has_c4_followup_plan=has_plan,
            plan_accepted=chosen.get("accepted"),
            adaptive=is_adaptive_profile(profile) if profile else None,
        )

    return {
        "original_distance_pct": distance_pct,
        "distance_bucket": bucket_raw,
        "theoretical_distance_bucket": theoretical,
        "distance_status": status,
        "grid_step_pct": chosen.get("grid_step_pct"),
        "requested_absolute_stage_distances_pct": chosen.get(
            "requested_absolute_stage_distances_pct"
        ),
        "effective_absolute_stage_distances_pct": chosen.get(
            "effective_absolute_stage_distances_pct"
        ),
        "requested_price_fractions": chosen.get("requested_price_fractions")
        or chosen.get("selected_price_fractions"),
        "effective_price_fractions": chosen.get("effective_price_fractions"),
        "requested_stage_count": chosen.get("requested_stage_count")
        or chosen.get("selected_stage_count"),
        "capped_stage_count": chosen.get("capped_stage_count"),
        "stage_cap_applied": int(bool(chosen.get("stage_cap_applied"))),
        "first_observed_distance_pct": distances[0] if distances else None,
        "last_observed_distance_pct": distances[-1] if distances else None,
        "max_observed_distance_pct": max(distances) if distances else None,
        "observed_plan_count": len(plans),
        "selected_stage_count": chosen.get("selected_stage_count"),
        "selected_price_fractions": chosen.get("selected_price_fractions"),
        "selected_qty_fractions": chosen.get("selected_qty_fractions"),
        "requested_qty_fractions": chosen.get("requested_qty_fractions")
        or chosen.get("selected_qty_fractions"),
        "effective_qty_fractions": chosen.get("effective_qty_fractions"),
        "effective_stage_count_after_rounding": effective,
        "skipped_small_stages": int(safe_float(chosen.get("skipped_small_stages"))),
        "merged_stage_count": int(safe_float(chosen.get("merged_stage_count"))),
        "residual_qty": chosen.get("residual_qty"),
        "fallback_used": chosen.get("fallback_used"),
        "adaptive_family": chosen.get("adaptive_family"),
        "fixed_step_qty_family": chosen.get("fixed_step_qty_family"),
        "stage_activation_count": len(planned_u),
        "stage_fill_count": len(filled_u),
        "first_stage_fill_delay": first_delay,
        "last_stage_fill_delay": (
            (max(fill_candles) - min(intent_candles))
            if intent_candles and fill_candles
            else None
        ),
        "filled_stage_indices": filled_u,
        "unfilled_stage_indices": unfilled,
    }


def finalize_row_distance_status(row: dict[str, Any]) -> dict[str, Any]:
    """Fill distance_status for rows missing live diagnostics (e.g. legacy / no C4)."""
    out = dict(row)
    prof = str(out.get("profile") or "")
    if out.get("distance_status"):
        return out
    d = _finite_float(out.get("original_distance_pct") or out.get("last_observed_distance_pct"))
    theo = out.get("theoretical_distance_bucket")
    if theo is None and d is not None:
        theo = theoretical_bucket_label(select_distance_bucket(d))
        out["theoretical_distance_bucket"] = theo
    has_followup = bool(
        out.get("observed_plan_count")
        or out.get("distance_bucket")
        or theo
        or (d is not None and d > 0)
    )
    out["distance_status"] = classify_distance_status(
        profile=prof,
        max_cycle=int(safe_float(out.get("max_cycle"))),
        distance_pct=d,
        bucket=out.get("distance_bucket") or theo,
        has_c4_followup_plan=has_followup,
        plan_accepted=None,
        adaptive=is_adaptive_profile(prof),
    )
    return out


def enrich_profile_row(row: dict[str, Any], result: Any | None = None) -> dict[str, Any]:
    """Attach adaptive diagnostics + exposure aliases onto a profile run row."""
    out = dict(row)
    if result is not None:
        diag = extract_adaptive_diagnostics(result)
        out.update(diag)
    out = finalize_row_distance_status(out)
    out.setdefault("merged_stage_count", 0)
    out.setdefault("observed_plan_count", int(safe_float(out.get("observed_plan_count"))))
    out["max_gross_exposure"] = safe_float(
        out.get("max_gross_exposure")
        or out.get("gross_exposure")
        or out.get("max_long_notional")
    )
    out["max_abs_net_exposure"] = safe_float(
        out.get("max_abs_net_exposure") or out.get("net_exposure")
    )
    out["worst_mtm"] = safe_float(out.get("worst_mtm"))
    out["open_mtm"] = safe_float(out.get("open_mtm"))
    out["closed_pnl"] = safe_float(out.get("closed_pnl") or out.get("realized_pnl"))
    out["total_pnl"] = safe_float(out.get("total_pnl"))
    out["is_historical_blocker"] = int(safe_float(out.get("is_historical_blocker")))
    out["economically_valid_close"] = int(safe_float(out.get("economically_valid_close")))
    out["staging_activated"] = int(safe_float(out.get("staging_activated")))
    for key in (
        "selected_price_fractions",
        "selected_qty_fractions",
        "requested_price_fractions",
        "effective_price_fractions",
        "requested_qty_fractions",
        "effective_qty_fractions",
        "requested_absolute_stage_distances_pct",
        "effective_absolute_stage_distances_pct",
        "filled_stage_indices",
        "unfilled_stage_indices",
    ):
        if isinstance(out.get(key), (list, tuple)):
            out[key] = list(out[key])
    return out


def compare_profiles(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    start_meta: dict[str, Any],
    *,
    baseline_name: str,
    candidate_name: str,
) -> dict[str, Any]:
    pair = compare_pair(baseline, candidate, start_meta)
    pair["baseline_profile"] = baseline_name
    pair["candidate_profile"] = candidate_name
    pair["window_id"] = start_meta.get("window_id") or baseline.get("window_id")
    pair["window_kind"] = start_meta.get("window_kind") or baseline.get("window_kind")
    pair["distance_bucket"] = summarize_bucket_key(candidate) or summarize_bucket_key(baseline)
    pair["theoretical_distance_bucket"] = candidate.get("theoretical_distance_bucket") or baseline.get(
        "theoretical_distance_bucket"
    )
    pair["distance_status"] = candidate.get("distance_status") or baseline.get("distance_status")
    pair["original_distance_pct"] = candidate.get("original_distance_pct") or baseline.get(
        "original_distance_pct"
    )
    pair["candidate_staging_activated"] = int(safe_float(candidate.get("staging_activated")))
    pair["candidate_effective_stage_count"] = candidate.get("effective_stage_count_after_rounding")
    pair["candidate_skipped_small_stages"] = candidate.get("skipped_small_stages")
    pair["candidate_max_gross_exposure"] = safe_float(candidate.get("max_gross_exposure"))
    pair["candidate_worst_mtm"] = safe_float(candidate.get("worst_mtm"))
    return pair


def summarize_by_distance_bucket(pairs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        groups[summarize_bucket_key(p)].append(p)
    rows = []
    for bucket, subset in sorted(groups.items()):
        s = summarize_pairs(subset)
        rows.append(
            {
                "distance_bucket": bucket,
                "n_pairs": s["n_pairs"],
                "sample_sufficient": int(s["n_pairs"] >= 10),
                "better": s["better"],
                "equal": s["equal"],
                "worse": s["worse"],
                "sum_delta_total_pnl": s["delta_total"]["sum"],
                "sum_delta_closed_pnl": s["sum_delta_closed_pnl"],
                "sum_delta_open_mtm": s["sum_delta_open_mtm"],
                "additional_valid_closes": s["additional_valid_closes"],
                "lost_valid_closes": s["lost_valid_closes"],
                "activated": sum(int(p.get("candidate_staging_activated") or 0) for p in subset),
            }
        )
    return rows


def summarize_by_profile_distance_bucket(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        prof = str(r.get("profile") or "")
        bucket = summarize_bucket_key(r, profile=prof)
        groups[(prof, bucket)].append(r)
    rows = []
    for (prof, bucket), subset in sorted(groups.items()):
        rows.append(
            {
                "profile": prof,
                "distance_bucket": bucket,
                "n_runs": len(subset),
                "sample_sufficient": int(len(subset) >= 10),
                "staging_activated": sum(int(safe_float(r.get("staging_activated"))) for r in subset),
                "sum_total_pnl": sum(safe_float(r.get("total_pnl")) for r in subset),
                "sum_closed_pnl": sum(safe_float(r.get("closed_pnl")) for r in subset),
                "sum_open_mtm": sum(safe_float(r.get("open_mtm")) for r in subset),
                "valid_closes": sum(int(safe_float(r.get("economically_valid_close"))) for r in subset),
                "mean_effective_stages": (
                    sum(safe_float(r.get("effective_stage_count_after_rounding")) for r in subset)
                    / len(subset)
                ),
                "sum_skipped_small_stages": sum(
                    int(safe_float(r.get("skipped_small_stages"))) for r in subset
                ),
            }
        )
    return rows


def summarize_by_distance_status(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        status = str(r.get("distance_status") or summarize_bucket_key(r))
        groups[status].append(r)
    rows = []
    for status, subset in sorted(groups.items()):
        rows.append(
            {
                "distance_status": status,
                "n_runs": len(subset),
                "staging_activated": sum(int(safe_float(r.get("staging_activated"))) for r in subset),
                "n_with_distance": sum(
                    1 for r in subset if _finite_float(r.get("original_distance_pct")) is not None
                ),
            }
        )
    return rows


def summarize_by_profile_distance_status(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        prof = str(r.get("profile") or "")
        status = str(r.get("distance_status") or summarize_bucket_key(r, profile=prof))
        groups[(prof, status)].append(r)
    rows = []
    for (prof, status), subset in sorted(groups.items()):
        rows.append(
            {
                "profile": prof,
                "distance_status": status,
                "n_runs": len(subset),
                "staging_activated": sum(int(safe_float(r.get("staging_activated"))) for r in subset),
                "n_with_distance": sum(
                    1 for r in subset if _finite_float(r.get("original_distance_pct")) is not None
                ),
            }
        )
    return rows


def bucket_activation_counts(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in raw_rows:
        if str(r.get("profile") or "") == "legacy":
            continue
        rows.append(r)
    counter: Counter[tuple[str, str, int]] = Counter()
    for r in rows:
        key = (
            str(r.get("profile") or ""),
            summarize_bucket_key(r),
            int(safe_float(r.get("staging_activated"))),
        )
        counter[key] += 1
    out = []
    for (prof, bucket, activated), n in sorted(counter.items()):
        out.append(
            {
                "profile": prof,
                "distance_bucket": bucket,
                "staging_activated": activated,
                "n": n,
            }
        )
    return out


def bucket_stage_fallbacks(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        if str(r.get("profile") or "") == "legacy":
            continue
        fb = str(r.get("fallback_used") or "none")
        groups[(str(r.get("profile") or ""), summarize_bucket_key(r), fb)].append(r)
    out = []
    for (prof, bucket, fb), subset in sorted(groups.items()):
        out.append(
            {
                "profile": prof,
                "distance_bucket": bucket,
                "fallback_used": fb,
                "n": len(subset),
                "mean_selected_stage_count": (
                    sum(safe_float(r.get("selected_stage_count")) for r in subset) / len(subset)
                ),
                "mean_effective_stage_count": (
                    sum(safe_float(r.get("effective_stage_count_after_rounding")) for r in subset)
                    / len(subset)
                ),
                "sum_skipped_small_stages": sum(
                    int(safe_float(r.get("skipped_small_stages"))) for r in subset
                ),
            }
        )
    return out


def blocker_summary_by_profile(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        groups[str(r.get("profile") or "")].append(r)
    out = []
    for prof, subset in sorted(groups.items()):
        blockers = [r for r in subset if int(safe_float(r.get("is_historical_blocker"))) == 1]
        closed = [r for r in blockers if int(safe_float(r.get("trade_flat"))) == 1]
        open_rows = [r for r in blockers if int(safe_float(r.get("trade_flat"))) == 0]
        out.append(
            {
                "profile": prof,
                "n_blocker_runs": len(blockers),
                "blocker_closed": len(closed),
                "blocker_open": len(open_rows),
                "blocker_valid_closes": sum(
                    int(safe_float(r.get("economically_valid_close"))) for r in blockers
                ),
                "sum_blocker_open_mtm": sum(safe_float(r.get("open_mtm")) for r in open_rows),
                "sum_blocker_total_pnl": sum(safe_float(r.get("total_pnl")) for r in blockers),
                "sum_blocker_closed_pnl": sum(safe_float(r.get("closed_pnl")) for r in blockers),
            }
        )
    return out


def blocker_summary_by_profile_bucket(raw_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for r in raw_rows:
        groups[(str(r.get("profile") or ""), summarize_bucket_key(r))].append(r)
    out = []
    for (prof, bucket), subset in sorted(groups.items()):
        blockers = [r for r in subset if int(safe_float(r.get("is_historical_blocker"))) == 1]
        closed = [r for r in blockers if int(safe_float(r.get("trade_flat"))) == 1]
        open_rows = [r for r in blockers if int(safe_float(r.get("trade_flat"))) == 0]
        out.append(
            {
                "profile": prof,
                "distance_bucket": bucket,
                "n_blocker_runs": len(blockers),
                "blocker_closed": len(closed),
                "blocker_open": len(open_rows),
                "blocker_valid_closes": sum(
                    int(safe_float(r.get("economically_valid_close"))) for r in blockers
                ),
                "sum_blocker_open_mtm": sum(safe_float(r.get("open_mtm")) for r in open_rows),
                "sum_blocker_total_pnl": sum(safe_float(r.get("total_pnl")) for r in blockers),
            }
        )
    return out


def exposure_drawdown_by_bucket(
    pairs: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pairs:
        groups[summarize_bucket_key(p)].append(p)
    rows = []
    for bucket, subset in sorted(groups.items()):
        d_exp = [
            safe_float(p.get("candidate_max_gross_exposure"))
            - safe_float(p.get("legacy_max_long_notional") or p.get("legacy_max_abs_net_exposure"))
            for p in subset
        ]
        d_dd = [
            safe_float(p.get("staging_max_drawdown_pct")) - safe_float(p.get("legacy_max_drawdown_pct"))
            for p in subset
        ]
        rows.append(
            {
                "distance_bucket": bucket,
                "n_pairs": len(subset),
                "sample_sufficient": int(len(subset) >= 10),
                "mean_delta_gross_exposure": (sum(d_exp) / len(d_exp)) if d_exp else None,
                "mean_delta_drawdown_pct": (sum(d_dd) / len(d_dd)) if d_dd else None,
                "sum_delta_open_mtm": sum(safe_float(p.get("delta_open_mtm")) for p in subset),
                "sum_delta_closed_pnl": sum(safe_float(p.get("delta_closed_pnl")) for p in subset),
            }
        )
    return rows


def comparison_by_distance_bucket(
    pairs: Sequence[dict[str, Any]],
    *,
    comparison_name: str,
) -> list[dict[str, Any]]:
    rows = summarize_by_distance_bucket(pairs)
    for r in rows:
        r["comparison"] = comparison_name
        if not r.get("sample_sufficient"):
            r["note"] = "sample_insufficient"
    return rows
