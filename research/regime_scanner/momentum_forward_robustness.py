"""Historical robustness analysis for Momentum forward outcomes.

Research-only. Reuses ``momentum_forward_audit`` without changing Regime / PA /
Momentum thresholds or adding entry / TP / SL logic.

Month assignment uses the primary-basis measurement timestamp (confirmed →
momentum candle; invalidated/expired/not_confirmed → PA candle).
"""

from __future__ import annotations

import argparse
import json
import math
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd

from .data_loader import load_symbol_candles
from .momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    COHORT_MOMENTUM_EXPIRED,
    COHORT_MOMENTUM_INVALIDATED,
    COHORT_MOMENTUM_NOT_CONFIRMED,
    DEFAULT_HORIZONS,
    PRIMARY_BASIS_BY_COHORT,
    aggregate_group,
    load_pipeline_artifacts,
    run_forward_audit,
)
from .pipeline_audit import run_pipeline_audit, write_pipeline_audit_outputs
from .point_audit import json_safe
from .signal_tp_audit import prepare_candle_window

FOCUS_HORIZON = 12
DEFAULT_MONTHS = (
    "2026-01",
    "2026-02",
    "2026-03",
    "2026-04",
    "2026-05",
    "2026-06",
)

SEGMENT_CONFIRMED_ALL = "confirmed_all"
SEGMENT_CONFIRMED_HIGH = "confirmed_high"
SEGMENT_CONFIRMED_AGE0 = "confirmed_age0"
SEGMENT_CONFIRMED_HIGH_AGE0 = "confirmed_high_age0"
SEGMENT_CONFIRMED_FBD = "confirmed_fbd"
SEGMENT_CONFIRMED_OTHER_PATTERNS = "confirmed_other_patterns"

def _age_is(row: dict[str, Any], expected: int) -> bool:
    age = row.get("confirmation_age")
    if age is None:
        return False
    try:
        return int(age) == int(expected)
    except (TypeError, ValueError):
        return False


SEGMENT_DEFS: dict[str, Callable[[dict[str, Any]], bool]] = {
    SEGMENT_CONFIRMED_ALL: lambda r: r.get("cohort") == COHORT_MOMENTUM_CONFIRMED,
    SEGMENT_CONFIRMED_HIGH: lambda r: (
        r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and r.get("momentum_confidence") == "high"
    ),
    SEGMENT_CONFIRMED_AGE0: lambda r: (
        r.get("cohort") == COHORT_MOMENTUM_CONFIRMED and _age_is(r, 0)
    ),
    SEGMENT_CONFIRMED_HIGH_AGE0: lambda r: (
        r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and r.get("momentum_confidence") == "high"
        and _age_is(r, 0)
    ),
    SEGMENT_CONFIRMED_FBD: lambda r: (
        r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and r.get("pattern_type") == "failed_breakdown"
    ),
    SEGMENT_CONFIRMED_OTHER_PATTERNS: lambda r: (
        r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
        and r.get("pattern_type") != "failed_breakdown"
    ),
}


def month_key_from_timestamp(value: object | None) -> str | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return f"{ts.year:04d}-{ts.month:02d}"


def primary_outcome_rows(outcome_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Primary-basis rows only; also synthesize not_confirmed combo rows once."""
    primary = [dict(r) for r in outcome_rows if r.get("is_primary_basis")]
    # Explicit not_confirmed combo: invalidated+expired on pa_candle (already primary).
    # Add synthetic cohort label copies so monthly tables can show the combo.
    combo: list[dict[str, Any]] = []
    for r in primary:
        if r.get("in_not_confirmed_combo") and r.get("measurement_basis") == "pa_candle":
            copy = dict(r)
            copy["cohort"] = COHORT_MOMENTUM_NOT_CONFIRMED
            copy["is_combo_row"] = True
            combo.append(copy)
    return primary + combo


def enrich_with_month(
    rows: list[dict[str, Any]],
    *,
    month_field: str = "month",
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        row = dict(r)
        row[month_field] = month_key_from_timestamp(r.get("measurement_timestamp"))
        out.append(row)
    return out


def dedupe_outcome_rows(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop duplicate (setup_id, cohort, measurement_basis, horizon, is_combo_row).

    Protects against accidental concatenation of overlapping monthly pipelines.
    """
    seen: set[tuple[Any, ...]] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        key = (
            r.get("setup_id"),
            r.get("cohort"),
            r.get("measurement_basis"),
            int(r.get("horizon") or -1),
            bool(r.get("is_combo_row")),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def summarize_forward_metrics(
    rows: list[dict[str, Any]],
    *,
    group_keys: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extend ``aggregate_group`` with MFE/MAE ratio and MFE>MAE share."""
    base = aggregate_group(rows, group_keys=group_keys or {})
    evaluable = [r for r in rows if r.get("evaluable") is True]
    if not evaluable:
        base["mfe_mae_ratio_median"] = None
        base["mfe_gt_mae_share"] = None
        return base

    ratios: list[float] = []
    gt = 0
    for r in evaluable:
        mfe = float(r["mfe_pct"])
        mae = float(r["mae_pct"])
        if mae > 0.0 and math.isfinite(mfe) and math.isfinite(mae):
            ratios.append(mfe / mae)
        if mfe > mae:
            gt += 1
    ratios_sorted = sorted(ratios)
    base["mfe_mae_ratio_median"] = (
        None
        if not ratios_sorted
        else (
            ratios_sorted[len(ratios_sorted) // 2]
            if len(ratios_sorted) % 2 == 1
            else 0.5
            * (
                ratios_sorted[len(ratios_sorted) // 2 - 1]
                + ratios_sorted[len(ratios_sorted) // 2]
            )
        )
    )
    base["mfe_gt_mae_share"] = gt / len(evaluable)
    return base


def filter_segment(
    rows: list[dict[str, Any]],
    segment: str,
) -> list[dict[str, Any]]:
    pred = SEGMENT_DEFS[segment]
    return [r for r in rows if pred(r)]


def build_monthly_cohort_summary(
    primary_rows: list[dict[str, Any]],
    *,
    months: Iterable[str] | None = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
) -> list[dict[str, Any]]:
    months_list = list(months) if months is not None else sorted(
        {r.get("month") for r in primary_rows if r.get("month")}
    )
    horizons_t = [int(h) for h in horizons]
    cohorts = (
        COHORT_MOMENTUM_CONFIRMED,
        COHORT_MOMENTUM_INVALIDATED,
        COHORT_MOMENTUM_EXPIRED,
        COHORT_MOMENTUM_NOT_CONFIRMED,
    )
    out: list[dict[str, Any]] = []
    for month in months_list:
        for cohort in cohorts:
            for horizon in horizons_t:
                rows = [
                    r
                    for r in primary_rows
                    if r.get("month") == month
                    and r.get("cohort") == cohort
                    and int(r.get("horizon") or -1) == horizon
                ]
                out.append(
                    summarize_forward_metrics(
                        rows,
                        group_keys={
                            "group_type": "monthly_cohort",
                            "month": month,
                            "cohort": cohort,
                            "horizon": horizon,
                            "scope": "month",
                        },
                    )
                )
    # Overall (all months) — separate rows, not a weighted blend of month medians
    for cohort in cohorts:
        for horizon in horizons_t:
            rows = [
                r
                for r in primary_rows
                if r.get("cohort") == cohort and int(r.get("horizon") or -1) == horizon
            ]
            out.append(
                summarize_forward_metrics(
                    rows,
                    group_keys={
                        "group_type": "overall_cohort",
                        "month": "ALL",
                        "cohort": cohort,
                        "horizon": horizon,
                        "scope": "overall",
                    },
                )
            )
    return out


def build_monthly_segment_summary(
    primary_rows: list[dict[str, Any]],
    *,
    months: Iterable[str] | None = None,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    focus_horizon: int = FOCUS_HORIZON,
) -> list[dict[str, Any]]:
    """Segment + side/pattern/confidence/age/regime slices by month and overall."""
    months_list = list(months) if months is not None else sorted(
        {r.get("month") for r in primary_rows if r.get("month")}
    )
    horizons_t = [int(h) for h in horizons]
    out: list[dict[str, Any]] = []

    # Confirmed quality segments
    for segment, _ in SEGMENT_DEFS.items():
        for scope_month in [*months_list, "ALL"]:
            for horizon in horizons_t:
                pool = primary_rows if scope_month == "ALL" else [
                    r for r in primary_rows if r.get("month") == scope_month
                ]
                rows = [
                    r
                    for r in filter_segment(pool, segment)
                    if int(r.get("horizon") or -1) == horizon
                ]
                out.append(
                    summarize_forward_metrics(
                        rows,
                        group_keys={
                            "group_type": "monthly_segment",
                            "month": scope_month,
                            "segment": segment,
                            "horizon": horizon,
                            "side": None,
                            "pattern_type": None,
                            "momentum_confidence": None,
                            "confirmation_age": None,
                            "combined_regime": None,
                            "scope": "overall" if scope_month == "ALL" else "month",
                        },
                    )
                )

    # Confirmed vs not_confirmed by month (focus horizon emphasized but all horizons)
    for scope_month in [*months_list, "ALL"]:
        for cohort in (COHORT_MOMENTUM_CONFIRMED, COHORT_MOMENTUM_NOT_CONFIRMED):
            for horizon in horizons_t:
                pool = primary_rows if scope_month == "ALL" else [
                    r for r in primary_rows if r.get("month") == scope_month
                ]
                rows = [
                    r
                    for r in pool
                    if r.get("cohort") == cohort and int(r.get("horizon") or -1) == horizon
                ]
                out.append(
                    summarize_forward_metrics(
                        rows,
                        group_keys={
                            "group_type": "confirmed_vs_not_confirmed",
                            "month": scope_month,
                            "segment": cohort,
                            "horizon": horizon,
                            "side": None,
                            "pattern_type": None,
                            "momentum_confidence": None,
                            "confirmation_age": None,
                            "combined_regime": None,
                            "scope": "overall" if scope_month == "ALL" else "month",
                        },
                    )
                )

    # Additional groupings on confirmed primary rows at all horizons
    slice_specs = (
        ("side", "side"),
        ("pattern_type", "pattern_type"),
        ("momentum_confidence", "momentum_confidence"),
        ("confirmation_age", "confirmation_age"),
        ("combined_regime", "combined_regime"),
    )
    for field, key_name in slice_specs:
        values = sorted(
            {
                r.get(field)
                for r in primary_rows
                if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
                and r.get(field) is not None
            },
            key=lambda x: str(x),
        )
        for scope_month in [*months_list, "ALL"]:
            for value in values:
                for horizon in horizons_t:
                    pool = primary_rows if scope_month == "ALL" else [
                        r for r in primary_rows if r.get("month") == scope_month
                    ]
                    rows = [
                        r
                        for r in pool
                        if r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
                        and r.get(field) == value
                        and int(r.get("horizon") or -1) == horizon
                    ]
                    keys = {
                        "group_type": f"monthly_by_{field}",
                        "month": scope_month,
                        "segment": f"confirmed_{field}={value}",
                        "horizon": horizon,
                        "side": value if field == "side" else None,
                        "pattern_type": value if field == "pattern_type" else None,
                        "momentum_confidence": value
                        if field == "momentum_confidence"
                        else None,
                        "confirmation_age": value if field == "confirmation_age" else None,
                        "combined_regime": value if field == "combined_regime" else None,
                        "scope": "overall" if scope_month == "ALL" else "month",
                    }
                    # unused key_name kept for clarity / future
                    _ = key_name
                    out.append(summarize_forward_metrics(rows, group_keys=keys))

    # Ensure focus horizon appears even for empty months (already covered by loops)
    _ = focus_horizon
    return out


def _lookup_metric(
    rows: list[dict[str, Any]],
    *,
    month: str,
    cohort: str,
    horizon: int,
    field: str,
) -> float | None:
    for r in rows:
        if (
            r.get("month") == month
            and r.get("cohort") == cohort
            and int(r.get("horizon") or -1) == horizon
            and r.get(field) is not None
        ):
            return float(r[field])
    return None


def _span(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"median": None, "min": None, "max": None, "range": None}
    s = sorted(values)
    med = (
        s[len(s) // 2]
        if len(s) % 2 == 1
        else 0.5 * (s[len(s) // 2 - 1] + s[len(s) // 2])
    )
    return {
        "median": med,
        "min": s[0],
        "max": s[-1],
        "range": s[-1] - s[0],
    }


def build_stability_summary(
    monthly_cohort: list[dict[str, Any]],
    monthly_segment: list[dict[str, Any]],
    *,
    months: Iterable[str],
    focus_horizon: int = FOCUS_HORIZON,
    primary_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    months_list = list(months)
    h = int(focus_horizon)

    mae_wins: list[str] = []
    mfe_wins: list[str] = []
    pos_wins: list[str] = []
    mae_oppose: list[str] = []
    mfe_oppose: list[str] = []
    pos_oppose: list[str] = []
    mae_deltas: list[float] = []
    mfe_deltas: list[float] = []
    pos_deltas: list[float] = []
    month_details: list[dict[str, Any]] = []

    for month in months_list:
        c_mae = _lookup_metric(
            monthly_cohort, month=month, cohort=COHORT_MOMENTUM_CONFIRMED, horizon=h, field="mae_median"
        )
        n_mae = _lookup_metric(
            monthly_cohort,
            month=month,
            cohort=COHORT_MOMENTUM_NOT_CONFIRMED,
            horizon=h,
            field="mae_median",
        )
        c_mfe = _lookup_metric(
            monthly_cohort, month=month, cohort=COHORT_MOMENTUM_CONFIRMED, horizon=h, field="mfe_median"
        )
        n_mfe = _lookup_metric(
            monthly_cohort,
            month=month,
            cohort=COHORT_MOMENTUM_NOT_CONFIRMED,
            horizon=h,
            field="mfe_median",
        )
        c_pos = _lookup_metric(
            monthly_cohort,
            month=month,
            cohort=COHORT_MOMENTUM_CONFIRMED,
            horizon=h,
            field="positive_directional_return_share",
        )
        n_pos = _lookup_metric(
            monthly_cohort,
            month=month,
            cohort=COHORT_MOMENTUM_NOT_CONFIRMED,
            horizon=h,
            field="positive_directional_return_share",
        )
        c_n = _lookup_metric(
            monthly_cohort, month=month, cohort=COHORT_MOMENTUM_CONFIRMED, horizon=h, field="n_evaluable"
        )
        n_n = _lookup_metric(
            monthly_cohort,
            month=month,
            cohort=COHORT_MOMENTUM_NOT_CONFIRMED,
            horizon=h,
            field="n_evaluable",
        )

        detail = {
            "month": month,
            "confirmed_n": c_n,
            "not_confirmed_n": n_n,
            "confirmed_mae_median": c_mae,
            "not_confirmed_mae_median": n_mae,
            "confirmed_mfe_median": c_mfe,
            "not_confirmed_mfe_median": n_mfe,
            "confirmed_pos_share": c_pos,
            "not_confirmed_pos_share": n_pos,
        }
        if c_mae is not None and n_mae is not None:
            d = c_mae - n_mae  # negative = confirmed better
            mae_deltas.append(d)
            detail["mae_delta_confirmed_minus_not"] = d
            if d < 0:
                mae_wins.append(month)
            elif d > 0:
                mae_oppose.append(month)
        if c_mfe is not None and n_mfe is not None:
            d = c_mfe - n_mfe  # positive = confirmed better
            mfe_deltas.append(d)
            detail["mfe_delta_confirmed_minus_not"] = d
            if d > 0:
                mfe_wins.append(month)
            elif d < 0:
                mfe_oppose.append(month)
        if c_pos is not None and n_pos is not None:
            d = c_pos - n_pos
            pos_deltas.append(d)
            detail["pos_share_delta_confirmed_minus_not"] = d
            if d > 0:
                pos_wins.append(month)
            elif d < 0:
                pos_oppose.append(month)
        month_details.append(detail)

    def _seg(month: str, segment: str) -> dict[str, Any] | None:
        for r in monthly_segment:
            if (
                r.get("group_type") == "monthly_segment"
                and r.get("month") == month
                and r.get("segment") == segment
                and int(r.get("horizon") or -1) == h
            ):
                return r
        return None

    # high vs medium consistency
    high_better_months: list[str] = []
    high_worse_months: list[str] = []
    age0_better_months: list[str] = []
    age0_worse_months: list[str] = []
    high_age0_vs_all: list[dict[str, Any]] = []
    fbd_vs_other: list[dict[str, Any]] = []

    for month in [*months_list, "ALL"]:
        high = next(
            (
                r
                for r in monthly_segment
                if r.get("group_type") == "monthly_by_momentum_confidence"
                and r.get("month") == month
                and r.get("momentum_confidence") == "high"
                and int(r.get("horizon") or -1) == h
            ),
            None,
        )
        med = next(
            (
                r
                for r in monthly_segment
                if r.get("group_type") == "monthly_by_momentum_confidence"
                and r.get("month") == month
                and r.get("momentum_confidence") == "medium"
                and int(r.get("horizon") or -1) == h
            ),
            None,
        )
        if month != "ALL" and high and med and high.get("mfe_median") is not None and med.get("mfe_median") is not None:
            if float(high["mfe_median"]) > float(med["mfe_median"]):
                high_better_months.append(month)
            elif float(high["mfe_median"]) < float(med["mfe_median"]):
                high_worse_months.append(month)

        age0 = next(
            (
                r
                for r in monthly_segment
                if r.get("group_type") == "monthly_by_confirmation_age"
                and r.get("month") == month
                and r.get("confirmation_age") == 0
                and int(r.get("horizon") or -1) == h
            ),
            None,
        )
        later = [
            r
            for r in monthly_segment
            if r.get("group_type") == "monthly_by_confirmation_age"
            and r.get("month") == month
            and r.get("confirmation_age") in {1, 2, 3}
            and r.get("mfe_median") is not None
            and int(r.get("horizon") or -1) == h
        ]
        if month != "ALL" and age0 and age0.get("mfe_median") is not None and later:
            later_avg = sum(float(r["mfe_median"]) for r in later) / len(later)
            if float(age0["mfe_median"]) > later_avg:
                age0_better_months.append(month)
            elif float(age0["mfe_median"]) < later_avg:
                age0_worse_months.append(month)

        seg_all = _seg(month, SEGMENT_CONFIRMED_ALL)
        seg_hq = _seg(month, SEGMENT_CONFIRMED_HIGH_AGE0)
        if seg_all and seg_hq:
            high_age0_vs_all.append(
                {
                    "month": month,
                    "all_mfe": seg_all.get("mfe_median"),
                    "high_age0_mfe": seg_hq.get("mfe_median"),
                    "all_mae": seg_all.get("mae_median"),
                    "high_age0_mae": seg_hq.get("mae_median"),
                    "all_pos": seg_all.get("positive_directional_return_share"),
                    "high_age0_pos": seg_hq.get("positive_directional_return_share"),
                    "all_n": seg_all.get("n_evaluable"),
                    "high_age0_n": seg_hq.get("n_evaluable"),
                }
            )
        fbd = _seg(month, SEGMENT_CONFIRMED_FBD)
        other = _seg(month, SEGMENT_CONFIRMED_OTHER_PATTERNS)
        if fbd and other:
            fbd_vs_other.append(
                {
                    "month": month,
                    "fbd_mfe": fbd.get("mfe_median"),
                    "other_mfe": other.get("mfe_median"),
                    "fbd_mae": fbd.get("mae_median"),
                    "other_mae": other.get("mae_median"),
                    "fbd_n": fbd.get("n_evaluable"),
                    "other_n": other.get("n_evaluable"),
                }
            )

    # Long vs short overall
    side_rows = [
        r
        for r in monthly_segment
        if r.get("group_type") == "monthly_by_side"
        and r.get("month") == "ALL"
        and int(r.get("horizon") or -1) == h
    ]
    # Regime slices overall
    regime_rows = [
        r
        for r in monthly_segment
        if r.get("group_type") == "monthly_by_combined_regime"
        and r.get("month") == "ALL"
        and int(r.get("horizon") or -1) == h
        and (r.get("n_evaluable") or 0) > 0
    ]

    overall_conf = next(
        (
            r
            for r in monthly_cohort
            if r.get("month") == "ALL"
            and r.get("cohort") == COHORT_MOMENTUM_CONFIRMED
            and int(r.get("horizon") or -1) == h
        ),
        None,
    )
    overall_not = next(
        (
            r
            for r in monthly_cohort
            if r.get("month") == "ALL"
            and r.get("cohort") == COHORT_MOMENTUM_NOT_CONFIRMED
            and int(r.get("horizon") or -1) == h
        ),
        None,
    )

    n_conf = int((overall_conf or {}).get("n_evaluable") or 0)
    n_not = int((overall_not or {}).get("n_evaluable") or 0)
    sample_ok_phase4 = n_conf >= 80 and n_not >= 30 and len(mae_wins) >= 4

    research_answers = _answer_robustness_questions(
        months=months_list,
        mae_wins=mae_wins,
        mae_oppose=mae_oppose,
        mfe_wins=mfe_wins,
        mfe_oppose=mfe_oppose,
        pos_wins=pos_wins,
        pos_oppose=pos_oppose,
        high_better_months=high_better_months,
        high_worse_months=high_worse_months,
        age0_better_months=age0_better_months,
        age0_worse_months=age0_worse_months,
        high_age0_vs_all=high_age0_vs_all,
        fbd_vs_other=fbd_vs_other,
        side_rows=side_rows,
        regime_rows=regime_rows,
        overall_conf=overall_conf,
        overall_not=overall_not,
        sample_ok_phase4=sample_ok_phase4,
        n_conf=n_conf,
        n_not=n_not,
    )

    return {
        "focus_horizon_candles": h,
        "months_analyzed": months_list,
        "month_details": month_details,
        "months_confirmed_lower_mae": mae_wins,
        "months_confirmed_higher_mae": mae_oppose,
        "months_confirmed_higher_mfe": mfe_wins,
        "months_confirmed_lower_mfe": mfe_oppose,
        "months_confirmed_higher_pos_share": pos_wins,
        "months_confirmed_lower_pos_share": pos_oppose,
        "n_months_mae_win": len(mae_wins),
        "n_months_mfe_win": len(mfe_wins),
        "n_months_pos_win": len(pos_wins),
        "n_months_compared": len(months_list),
        "mae_delta_span": _span(mae_deltas),
        "mfe_delta_span": _span(mfe_deltas),
        "pos_share_delta_span": _span(pos_deltas),
        "high_better_mfe_months": high_better_months,
        "high_worse_mfe_months": high_worse_months,
        "age0_better_mfe_months": age0_better_months,
        "age0_worse_mfe_months": age0_worse_months,
        "high_age0_vs_all": high_age0_vs_all,
        "fbd_vs_other": fbd_vs_other,
        "side_overall": side_rows,
        "regime_overall": regime_rows,
        "overall_confirmed": overall_conf,
        "overall_not_confirmed": overall_not,
        "sample_counts": {"confirmed": n_conf, "not_confirmed": n_not},
        "sample_ok_for_phase4": sample_ok_phase4,
        "research_answers": research_answers,
        "signal_counts_by_month": _signal_counts_by_month(primary_rows or []),
    }


def _signal_counts_by_month(primary_rows: list[dict[str, Any]]) -> dict[str, Any]:
    # Count unique setup_ids per cohort×month at any one horizon to avoid ×horizons inflate
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    seen: set[tuple[Any, ...]] = set()
    for r in primary_rows:
        if int(r.get("horizon") or -1) != FOCUS_HORIZON:
            continue
        key = (r.get("month"), r.get("cohort"), r.get("setup_id"), bool(r.get("is_combo_row")))
        if key in seen:
            continue
        seen.add(key)
        month = r.get("month") or "UNKNOWN"
        counts[str(month)][str(r.get("cohort"))] += 1
    return {m: dict(v) for m, v in sorted(counts.items())}


def _answer_robustness_questions(
    *,
    months: list[str],
    mae_wins: list[str],
    mae_oppose: list[str],
    mfe_wins: list[str],
    mfe_oppose: list[str],
    pos_wins: list[str],
    pos_oppose: list[str],
    high_better_months: list[str],
    high_worse_months: list[str],
    age0_better_months: list[str],
    age0_worse_months: list[str],
    high_age0_vs_all: list[dict[str, Any]],
    fbd_vs_other: list[dict[str, Any]],
    side_rows: list[dict[str, Any]],
    regime_rows: list[dict[str, Any]],
    overall_conf: dict[str, Any] | None,
    overall_not: dict[str, Any] | None,
    sample_ok_phase4: bool,
    n_conf: int,
    n_not: int,
) -> dict[str, Any]:
    n_m = len(months)
    mae_stable = len(mae_wins) >= max(1, (n_m + 1) // 2) and len(mae_oppose) <= len(mae_wins)
    mfe_adv = len(mfe_wins) > len(mfe_oppose)
    pos_adv = len(pos_wins) > len(pos_oppose)

    overall_hq = next((x for x in high_age0_vs_all if x.get("month") == "ALL"), None)
    hq_better = False
    if overall_hq and overall_hq.get("high_age0_mfe") is not None and overall_hq.get("all_mfe") is not None:
        hq_better = float(overall_hq["high_age0_mfe"]) > float(overall_hq["all_mfe"]) and (
            overall_hq.get("high_age0_mae") is None
            or overall_hq.get("all_mae") is None
            or float(overall_hq["high_age0_mae"]) <= float(overall_hq["all_mae"])
        )

    overall_fbd = next((x for x in fbd_vs_other if x.get("month") == "ALL"), None)
    fbd_stronger = False
    if (
        overall_fbd
        and overall_fbd.get("fbd_mfe") is not None
        and overall_fbd.get("other_mfe") is not None
    ):
        fbd_stronger = float(overall_fbd["fbd_mfe"]) > float(overall_fbd["other_mfe"])

    long_r = next((r for r in side_rows if r.get("side") == "long"), None)
    short_r = next((r for r in side_rows if r.get("side") == "short"), None)
    side_diff = None
    if (
        long_r
        and short_r
        and long_r.get("mfe_median") is not None
        and short_r.get("mfe_median") is not None
    ):
        side_diff = abs(float(long_r["mfe_median"]) - float(short_r["mfe_median"]))

    # Regimes where confirmed pos share or mfe looks relatively strong
    regime_ranked = sorted(
        [r for r in regime_rows if r.get("mfe_median") is not None],
        key=lambda r: float(r["mfe_median"]),
        reverse=True,
    )[:5]

    # Recommendation heuristic (research wording only)
    if mae_stable and not mfe_adv and not pos_adv:
        recommendation = "risk_indicator"
    elif mae_stable and (hq_better or (len(high_better_months) > len(high_worse_months))):
        recommendation = "confidence_score"
    elif mae_stable and mfe_adv and pos_adv and sample_ok_phase4:
        recommendation = "hard_filter_candidate"
    else:
        recommendation = "research_only_keep_observing"

    return {
        "q1_mae_advantage_stable": {
            "answer": "yes" if mae_stable else "no",
            "months_win": mae_wins,
            "months_oppose": mae_oppose,
            "win_count": f"{len(mae_wins)}/{n_m}",
        },
        "q2_long_term_mfe_or_return_advantage": {
            "mfe_advantage": "yes" if mfe_adv else "no",
            "pos_share_advantage": "yes" if pos_adv else "no",
            "mfe_months_win": mfe_wins,
            "mfe_months_oppose": mfe_oppose,
            "pos_months_win": pos_wins,
            "pos_months_oppose": pos_oppose,
            "overall_confirmed_mfe": (overall_conf or {}).get("mfe_median"),
            "overall_not_confirmed_mfe": (overall_not or {}).get("mfe_median"),
            "overall_confirmed_pos": (overall_conf or {}).get(
                "positive_directional_return_share"
            ),
            "overall_not_confirmed_pos": (overall_not or {}).get(
                "positive_directional_return_share"
            ),
        },
        "q3_high_better_than_medium": {
            "answer": (
                "yes"
                if len(high_better_months) > len(high_worse_months)
                else "no"
                if len(high_worse_months) > len(high_better_months)
                else "mixed"
            ),
            "months_high_better_mfe": high_better_months,
            "months_high_worse_mfe": high_worse_months,
        },
        "q4_age0_better_than_later": {
            "answer": (
                "yes"
                if len(age0_better_months) > len(age0_worse_months)
                else "no"
                if len(age0_worse_months) > len(age0_better_months)
                else "mixed"
            ),
            "months_age0_better": age0_better_months,
            "months_age0_worse": age0_worse_months,
        },
        "q5_high_and_age0_stable_quality_filter": {
            "answer": "yes" if hq_better else "no",
            "overall": overall_hq,
        },
        "q6_fbd_stronger_than_other_patterns": {
            "answer": "yes" if fbd_stronger else "no",
            "overall": overall_fbd,
        },
        "q7_long_vs_short_difference": {
            "answer": (
                "material"
                if side_diff is not None and side_diff >= 0.15
                else "modest"
                if side_diff is not None
                else "insufficient_data"
            ),
            "abs_mfe_median_gap": side_diff,
            "long": long_r,
            "short": short_r,
        },
        "q8_regimes_where_momentum_helps": {
            "top_confirmed_mfe_regimes": [
                {
                    "combined_regime": r.get("combined_regime"),
                    "mfe_median": r.get("mfe_median"),
                    "mae_median": r.get("mae_median"),
                    "pos_share": r.get("positive_directional_return_share"),
                    "n": r.get("n_evaluable"),
                }
                for r in regime_ranked
            ],
        },
        "q9_sample_enough_for_phase4": {
            "answer": "yes" if sample_ok_phase4 else "no",
            "n_confirmed": n_conf,
            "n_not_confirmed": n_not,
            "note": "heuristic: confirmed>=80, not_confirmed>=30, mae win months>=4",
        },
        "q10_recommended_momentum_role": {
            "answer": recommendation,
            "rationale": (
                "MAE filter-like behavior without consistent MFE/return edge → risk indicator; "
                "quality slices (high / age0) matter → confidence score; "
                "only hard filter if MAE+MFE+pos all stable with adequate n."
            ),
        },
    }


def run_robustness_from_outcomes(
    outcome_rows: list[dict[str, Any]],
    *,
    months: Iterable[str] = DEFAULT_MONTHS,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    focus_horizon: int = FOCUS_HORIZON,
) -> dict[str, Any]:
    months_list = list(months)
    horizons_t = tuple(int(h) for h in horizons)
    primary = enrich_with_month(primary_outcome_rows(outcome_rows))
    primary = dedupe_outcome_rows(primary)
    # Restrict analysis months if requested, but keep ALL overall from filtered set
    primary_in_scope = [
        r for r in primary if r.get("month") in set(months_list) or r.get("month") is None
    ]
    # Drop unknown month from monthly tables but keep in overall? Prefer exclude unknown.
    primary_known = [r for r in primary_in_scope if r.get("month") in set(months_list)]

    monthly_cohort = build_monthly_cohort_summary(
        primary_known, months=months_list, horizons=horizons_t
    )
    monthly_segment = build_monthly_segment_summary(
        primary_known,
        months=months_list,
        horizons=horizons_t,
        focus_horizon=focus_horizon,
    )
    stability = build_stability_summary(
        monthly_cohort,
        monthly_segment,
        months=months_list,
        focus_horizon=focus_horizon,
        primary_rows=primary_known,
    )
    return {
        "all_signal_forward_outcomes": primary_known,
        "monthly_cohort_summary": monthly_cohort,
        "monthly_segment_summary": monthly_segment,
        "stability_summary": stability,
        "meta": {
            "months": months_list,
            "horizons": list(horizons_t),
            "focus_horizon": focus_horizon,
            "n_primary_outcome_rows": len(primary_known),
            "n_unique_setups": len({r.get("setup_id") for r in primary_known}),
        },
    }


def run_robustness_analysis(
    *,
    symbol: str = "APTUSDT",
    start: str = "2026-01-01",
    end: str = "2026-06-28",
    months: Iterable[str] = DEFAULT_MONTHS,
    pipeline_dir: str | Path | None = None,
    skip_pipeline: bool = False,
    workers: int = 4,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    focus_horizon: int = FOCUS_HORIZON,
) -> dict[str, Any]:
    """Run (or load) pipeline → forward audit → monthly robustness."""
    t0 = time.perf_counter()
    pipe_dir = Path(
        pipeline_dir
        or "research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1"
    )
    runtime: dict[str, Any] = {"symbol": symbol, "start": start, "end": end}

    if not skip_pipeline:
        t_pipe = time.perf_counter()
        pipe = run_pipeline_audit(
            symbol=symbol,
            start=start,
            end=end,
            workers=workers,
            enable_momentum=True,
            progress_every=2048,
        )
        write_pipeline_audit_outputs(pipe, pipe_dir)
        runtime["pipeline_seconds"] = time.perf_counter() - t_pipe
        arts = {
            "price_action_confirmations": pipe.get("price_action_confirmations") or [],
            "momentum_confirmations": (pipe.get("momentum") or {}).get(
                "momentum_confirmations"
            )
            or [],
            "momentum_events": (pipe.get("momentum") or {}).get("momentum_events") or [],
        }
        candles = pipe.get("candles")
        if candles is None:
            raw = load_symbol_candles(symbol)
            prepared = prepare_candle_window(
                raw, start=start, end=end, history_candles=144, timeframes="5m,15m,30m"
            )
            candles = prepared["candles"]
    else:
        arts = load_pipeline_artifacts(pipe_dir)
        raw = load_symbol_candles(symbol)
        prepared = prepare_candle_window(
            raw, start=start, end=end, history_candles=144, timeframes="5m,15m,30m"
        )
        candles = prepared["candles"]
        runtime["pipeline_seconds"] = None

    t_fwd = time.perf_counter()
    forward = run_forward_audit(
        price_action_confirmations=arts["price_action_confirmations"],
        momentum_confirmations=arts["momentum_confirmations"],
        momentum_events=arts["momentum_events"],
        candles=candles,
        horizons=horizons,
    )
    runtime["forward_seconds"] = time.perf_counter() - t_fwd

    t_rob = time.perf_counter()
    robustness = run_robustness_from_outcomes(
        forward["signal_forward_outcomes"],
        months=months,
        horizons=horizons,
        focus_horizon=focus_horizon,
    )
    runtime["robustness_seconds"] = time.perf_counter() - t_rob
    runtime["total_seconds"] = time.perf_counter() - t0
    runtime["n_candles"] = int(len(candles))
    runtime["n_pa_confirmations"] = len(arts["price_action_confirmations"])
    runtime["n_momentum_confirmations"] = len(arts["momentum_confirmations"])
    runtime["pipeline_dir"] = str(pipe_dir)

    robustness["runtime"] = runtime
    robustness["forward_audit_summary"] = forward.get("audit_summary")
    return robustness


def format_robustness_readme(payload: dict[str, Any]) -> str:
    stab = payload.get("stability_summary") or {}
    a = stab.get("research_answers") or {}
    runtime = payload.get("runtime") or {}
    meta = payload.get("meta") or {}

    def _yn(block: dict[str, Any] | None, key: str = "answer") -> str:
        if not block:
            return "n/a"
        return str(block.get(key))

    lines = [
        "# Momentum Forward Robustness Audit",
        "",
        "Historical stability of Phase-3 momentum forward outcomes. "
        "No entry / TP / SL / threshold changes.",
        "",
        "## Data & runtime",
        "",
        f"- Symbol window: `{runtime.get('symbol')}` `{runtime.get('start')}` → `{runtime.get('end')}`",
        f"- Candles: `{runtime.get('n_candles')}`",
        f"- PA confirmations: `{runtime.get('n_pa_confirmations')}`, "
        f"Momentum confirmations: `{runtime.get('n_momentum_confirmations')}`",
        f"- Months: `{meta.get('months')}`",
        f"- Focus horizon: **{stab.get('focus_horizon_candles')}** candles",
        f"- Runtime seconds: pipeline=`{runtime.get('pipeline_seconds')}`, "
        f"forward=`{runtime.get('forward_seconds')}`, "
        f"robustness=`{runtime.get('robustness_seconds')}`, "
        f"total=`{runtime.get('total_seconds')}`",
        f"- Pipeline artifacts: `{runtime.get('pipeline_dir')}`",
        "",
        "## Signal counts by month",
        "",
        f"```json\n{json.dumps(stab.get('signal_counts_by_month'), indent=2)}\n```",
        "",
        "## Stability (confirmed vs not_confirmed)",
        "",
        f"- Lower MAE months: **{stab.get('n_months_mae_win')}/{stab.get('n_months_compared')}** "
        f"`{stab.get('months_confirmed_lower_mae')}` "
        f"(oppose: `{stab.get('months_confirmed_higher_mae')}`)",
        f"- Higher MFE months: **{stab.get('n_months_mfe_win')}/{stab.get('n_months_compared')}** "
        f"`{stab.get('months_confirmed_higher_mfe')}` "
        f"(oppose: `{stab.get('months_confirmed_lower_mfe')}`)",
        f"- Higher pos-return months: **{stab.get('n_months_pos_win')}/{stab.get('n_months_compared')}** "
        f"`{stab.get('months_confirmed_higher_pos_share')}` "
        f"(oppose: `{stab.get('months_confirmed_lower_pos_share')}`)",
        f"- MAE delta span (confirmed−not): `{stab.get('mae_delta_span')}`",
        f"- MFE delta span: `{stab.get('mfe_delta_span')}`",
        f"- Pos-share delta span: `{stab.get('pos_share_delta_span')}`",
        "",
        "## Research answers",
        "",
        "### 1. Ist die geringere MAE von confirmed über Monate stabil?",
        f"- **{_yn(a.get('q1_mae_advantage_stable'))}** — {a.get('q1_mae_advantage_stable')}",
        "",
        "### 2. Zeigt confirmed langfristig einen MFE- oder Return-Vorteil?",
        f"- {a.get('q2_long_term_mfe_or_return_advantage')}",
        "",
        "### 3. Ist confidence=high konsistent besser als medium?",
        f"- **{_yn(a.get('q3_high_better_than_medium'))}** — {a.get('q3_high_better_than_medium')}",
        "",
        "### 4. Ist Age 0 konsistent besser als spätere Bestätigung?",
        f"- **{_yn(a.get('q4_age0_better_than_later'))}** — {a.get('q4_age0_better_than_later')}",
        "",
        "### 5. Ist high + Age 0 ein stabiler Qualitätsfilter?",
        f"- **{_yn(a.get('q5_high_and_age0_stable_quality_filter'))}** — "
        f"{a.get('q5_high_and_age0_stable_quality_filter')}",
        "",
        "### 6. Ist FBD stabil stärker als andere PA-Typen?",
        f"- **{_yn(a.get('q6_fbd_stronger_than_other_patterns'))}** — "
        f"{a.get('q6_fbd_stronger_than_other_patterns')}",
        "",
        "### 7. Gibt es starke Unterschiede zwischen Long und Short?",
        f"- {a.get('q7_long_vs_short_difference')}",
        "",
        "### 8. Regime mit Momentum-Mehrwert?",
        f"- {a.get('q8_regimes_where_momentum_helps')}",
        "",
        "### 9. Fallzahlen ausreichend für Phase 4?",
        f"- **{_yn(a.get('q9_sample_enough_for_phase4'))}** — {a.get('q9_sample_enough_for_phase4')}",
        "",
        "### 10. Pflichtfilter, Confidence-Score oder Risikoindikator?",
        f"- **{_yn(a.get('q10_recommended_momentum_role'))}** — "
        f"{a.get('q10_recommended_momentum_role')}",
        "",
        "## Method notes",
        "",
        "- Reuses `momentum_forward_audit` metrics and primary measurement bases.",
        "- Monthly medians are computed **within each month**; overall rows are "
        "separate unweighted-by-month aggregations over all signals (not averages of monthly medians).",
        "- Duplicate `(setup_id, cohort, basis, horizon)` rows are dropped.",
        "- Special segments are post-hoc research slices only (no live filters).",
        "",
    ]
    return "\n".join(lines)


def write_robustness_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "outcomes_csv": out / "all_signal_forward_outcomes.csv",
        "monthly_cohort_csv": out / "monthly_cohort_summary.csv",
        "monthly_segment_csv": out / "monthly_segment_summary.csv",
        "stability_json": out / "stability_summary.json",
        "readme": out / "README.md",
        "outcomes_json": out / "all_signal_forward_outcomes.json",
        "runtime_json": out / "runtime.json",
    }
    pd.DataFrame(json_safe(payload.get("all_signal_forward_outcomes") or [])).to_csv(
        paths["outcomes_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("monthly_cohort_summary") or [])).to_csv(
        paths["monthly_cohort_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("monthly_segment_summary") or [])).to_csv(
        paths["monthly_segment_csv"], index=False
    )
    paths["stability_json"].write_text(
        json.dumps(json_safe(payload.get("stability_summary") or {}), indent=2),
        encoding="utf-8",
    )
    paths["readme"].write_text(format_robustness_readme(payload), encoding="utf-8")
    paths["outcomes_json"].write_text(
        json.dumps(json_safe(payload.get("all_signal_forward_outcomes") or []), indent=2),
        encoding="utf-8",
    )
    paths["runtime_json"].write_text(
        json.dumps(json_safe(payload.get("runtime") or {}), indent=2),
        encoding="utf-8",
    )
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Historical robustness of Momentum forward outcomes (research-only)."
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-01-01")
    p.add_argument("--end", default="2026-06-28")
    p.add_argument(
        "--pipeline-dir",
        default="research/backtests/results/regime_scanner_pipeline_audit_aptusdt_2026_h1",
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_momentum_forward_robustness",
    )
    p.add_argument("--workers", type=int, default=4)
    p.add_argument(
        "--skip-pipeline",
        action="store_true",
        help="Reuse existing pipeline artifacts in --pipeline-dir",
    )
    p.add_argument(
        "--months",
        default=",".join(DEFAULT_MONTHS),
        help="Comma-separated YYYY-MM months to include",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    months = tuple(m.strip() for m in str(args.months).split(",") if m.strip())
    payload = run_robustness_analysis(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        months=months,
        pipeline_dir=args.pipeline_dir,
        skip_pipeline=bool(args.skip_pipeline),
        workers=int(args.workers),
    )
    paths = write_robustness_outputs(payload, args.output_dir)
    stab = payload.get("stability_summary") or {}
    print(
        f"Robustness done: months={stab.get('months_analyzed')} "
        f"mae_wins={stab.get('n_months_mae_win')} "
        f"role={((stab.get('research_answers') or {}).get('q10_recommended_momentum_role') or {}).get('answer')} "
        f"runtime={payload.get('runtime')}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
