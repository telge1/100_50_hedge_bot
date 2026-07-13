"""TP-hit audit for Momentum forward signals (research-only).

Reuses measurement bases from ``momentum_forward_audit``:
* confirmed → momentum confirmation candle close
* not_confirmed → PA confirmation candle close

Does **not** change Regime / PA / Momentum logic, does not add SL or live entries.
Primary TP threshold: 0.25%. Additional analytic thresholds: 0.15 / 0.35 / 0.50.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from .data_loader import load_symbol_candles
from .momentum_forward_audit import (
    COHORT_MOMENTUM_CONFIRMED,
    COHORT_MOMENTUM_EXPIRED,
    COHORT_MOMENTUM_INVALIDATED,
    COHORT_MOMENTUM_NOT_CONFIRMED,
    DEFAULT_HORIZONS,
    PRIMARY_BASIS_BY_COHORT,
    _candle_maps,
    _ts_str,
    build_signal_rows,
    directional_close_return_pct,
    excursion_pcts_for_candle,
    load_pipeline_artifacts,
    ohlc_valid,
)
from .momentum_forward_robustness import _age_is
from .point_audit import json_safe
from .signal_tp_audit import prepare_candle_window

PRIMARY_TP_PCT = 0.25
ANALYTIC_TP_THRESHOLDS: tuple[float, ...] = (0.15, 0.25, 0.35, 0.50)
FOCUS_HORIZON = 12

GROUP_MOMENTUM_CONFIRMED = "momentum_confirmed"
GROUP_MOMENTUM_NOT_CONFIRMED = "momentum_not_confirmed"
GROUP_CONFIRMED_HIGH = "confirmed_high"
GROUP_CONFIRMED_AGE0 = "confirmed_age0"
GROUP_CONFIRMED_HIGH_AGE0 = "confirmed_high_age0"

GROUP_ORDER = (
    GROUP_MOMENTUM_CONFIRMED,
    GROUP_MOMENTUM_NOT_CONFIRMED,
    GROUP_CONFIRMED_HIGH,
    GROUP_CONFIRMED_AGE0,
    GROUP_CONFIRMED_HIGH_AGE0,
)


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    w = pos - lo
    return sorted_vals[lo] * (1.0 - w) + sorted_vals[hi] * w


def compute_tp_hit_for_path(
    *,
    side: str,
    reference_close: float,
    future_candles: list[dict[str, Any]],
    horizon: int,
    tp_pct: float,
) -> dict[str, Any]:
    """Evaluate TP hit within ``horizon`` candles after the measurement candle.

    Hit age is **0-based** among future candles (first candle after measurement = 0).

    Same-candle ambiguity: first candle that reaches ``tp_pct`` favorable also
    shows adverse excursion > 0 on that same candle → no intrabar order assumed.
    MAE-before-TP uses only candles *strictly before* the hit candle.
    """
    if horizon <= 0:
        raise ValueError("horizon must be positive")
    if tp_pct < 0:
        raise ValueError("tp_pct must be non-negative")

    if len(future_candles) < horizon:
        return {
            "evaluable": False,
            "reason": "INSUFFICIENT_FUTURE_CANDLES",
            "tp_hit": False,
            "same_candle_ambiguous": False,
            "first_hit_age": None,
            "mae_before_tp_pct": None,
            "directional_close_return_at_hit_pct": None,
            "hit_candle_timestamp": None,
            "available_future_candles": len(future_candles),
        }

    window = future_candles[:horizon]
    invalid = sum(1 for c in window if not ohlc_valid(c))
    if invalid:
        return {
            "evaluable": False,
            "reason": "INVALID_OHLC_IN_FORWARD_WINDOW",
            "tp_hit": False,
            "same_candle_ambiguous": False,
            "first_hit_age": None,
            "mae_before_tp_pct": None,
            "directional_close_return_at_hit_pct": None,
            "hit_candle_timestamp": None,
            "available_future_candles": len(future_candles),
            "invalid_ohlc_count": invalid,
        }

    mae_before = 0.0
    for age, candle in enumerate(window):
        fav, adv = excursion_pcts_for_candle(
            side=side,
            reference_close=reference_close,
            high=float(candle["high"]),
            low=float(candle["low"]),
        )
        fav = max(0.0, fav)
        adv = max(0.0, adv)
        if fav + 1e-15 >= tp_pct:  # exact threshold counts as hit
            ambiguous = adv > 0.0
            dret = directional_close_return_pct(
                side=side,
                reference_close=reference_close,
                future_close=float(candle["close"]),
            )
            return {
                "evaluable": True,
                "reason": None,
                "tp_hit": True,
                "same_candle_ambiguous": ambiguous,
                "first_hit_age": age,
                "mae_before_tp_pct": mae_before,
                "directional_close_return_at_hit_pct": dret,
                "hit_candle_timestamp": candle.get("timestamp"),
                "available_future_candles": len(future_candles),
                "invalid_ohlc_count": 0,
            }
        mae_before = max(mae_before, adv)

    return {
        "evaluable": True,
        "reason": None,
        "tp_hit": False,
        "same_candle_ambiguous": False,
        "first_hit_age": None,
        "mae_before_tp_pct": None,
        "directional_close_return_at_hit_pct": None,
        "hit_candle_timestamp": None,
        "available_future_candles": len(future_candles),
        "invalid_ohlc_count": 0,
    }


def signal_groups(signal: dict[str, Any]) -> list[str]:
    """Return group labels this signal belongs to (post-hoc research slices)."""
    groups: list[str] = []
    cohort = signal.get("cohort")
    if cohort == COHORT_MOMENTUM_CONFIRMED:
        groups.append(GROUP_MOMENTUM_CONFIRMED)
        if signal.get("momentum_confidence") == "high":
            groups.append(GROUP_CONFIRMED_HIGH)
        if _age_is(signal, 0):
            groups.append(GROUP_CONFIRMED_AGE0)
        if signal.get("momentum_confidence") == "high" and _age_is(signal, 0):
            groups.append(GROUP_CONFIRMED_HIGH_AGE0)
    if cohort in {COHORT_MOMENTUM_INVALIDATED, COHORT_MOMENTUM_EXPIRED} or signal.get(
        "in_not_confirmed_combo"
    ):
        groups.append(GROUP_MOMENTUM_NOT_CONFIRMED)
    return groups


def evaluate_signal_tp_hits(
    signal: dict[str, Any],
    *,
    candles: list[dict[str, Any]],
    ts_to_i: dict[str, int],
    horizons: Iterable[int],
    tp_thresholds: Iterable[float],
) -> list[dict[str, Any]]:
    cohort = str(signal.get("cohort"))
    basis = PRIMARY_BASIS_BY_COHORT.get(cohort)
    if basis is None:
        return []
    if basis == "momentum_candle":
        measure_ts = signal.get("momentum_confirmation_timestamp")
    else:
        measure_ts = signal.get("pa_structure_break_timestamp")

    groups = signal_groups(signal)
    base_meta = {
        "setup_id": signal.get("setup_id"),
        "side": signal.get("side"),
        "pattern_type": signal.get("pattern_type"),
        "cohort": cohort,
        "measurement_basis": basis,
        "measurement_timestamp": measure_ts,
        "momentum_confidence": signal.get("momentum_confidence"),
        "confirmation_age": signal.get("confirmation_age"),
        "groups": groups,
        "is_primary_tp": True,
    }

    out: list[dict[str, Any]] = []
    if not measure_ts:
        for h in horizons:
            for tp in tp_thresholds:
                out.append(
                    {
                        **base_meta,
                        "horizon": int(h),
                        "tp_pct": float(tp),
                        "is_primary_tp": abs(float(tp) - PRIMARY_TP_PCT) < 1e-12,
                        "evaluable": False,
                        "reason": "MISSING_MEASUREMENT_TIMESTAMP",
                        "tp_hit": False,
                        "same_candle_ambiguous": False,
                        "first_hit_age": None,
                        "mae_before_tp_pct": None,
                        "directional_close_return_at_hit_pct": None,
                        "hit_candle_timestamp": None,
                        "reference_close": None,
                        "available_future_candles": 0,
                    }
                )
        return out

    key = _ts_str(measure_ts)
    if key not in ts_to_i:
        for h in horizons:
            for tp in tp_thresholds:
                out.append(
                    {
                        **base_meta,
                        "horizon": int(h),
                        "tp_pct": float(tp),
                        "is_primary_tp": abs(float(tp) - PRIMARY_TP_PCT) < 1e-12,
                        "evaluable": False,
                        "reason": "MEASUREMENT_CANDLE_NOT_IN_FRAME",
                        "tp_hit": False,
                        "same_candle_ambiguous": False,
                        "first_hit_age": None,
                        "mae_before_tp_pct": None,
                        "directional_close_return_at_hit_pct": None,
                        "hit_candle_timestamp": None,
                        "reference_close": None,
                        "available_future_candles": 0,
                    }
                )
        return out

    i0 = ts_to_i[key]
    measure_candle = candles[i0]
    if not ohlc_valid(measure_candle):
        for h in horizons:
            for tp in tp_thresholds:
                out.append(
                    {
                        **base_meta,
                        "horizon": int(h),
                        "tp_pct": float(tp),
                        "is_primary_tp": abs(float(tp) - PRIMARY_TP_PCT) < 1e-12,
                        "evaluable": False,
                        "reason": "INVALID_MEASUREMENT_OHLC",
                        "tp_hit": False,
                        "same_candle_ambiguous": False,
                        "first_hit_age": None,
                        "mae_before_tp_pct": None,
                        "directional_close_return_at_hit_pct": None,
                        "hit_candle_timestamp": None,
                        "reference_close": None,
                        "available_future_candles": max(0, len(candles) - i0 - 1),
                    }
                )
        return out

    reference_close = float(measure_candle["close"])
    future = candles[i0 + 1 :]

    for h in horizons:
        for tp in tp_thresholds:
            metrics = compute_tp_hit_for_path(
                side=str(signal["side"]),
                reference_close=reference_close,
                future_candles=future,
                horizon=int(h),
                tp_pct=float(tp),
            )
            out.append(
                {
                    **base_meta,
                    "horizon": int(h),
                    "tp_pct": float(tp),
                    "is_primary_tp": abs(float(tp) - PRIMARY_TP_PCT) < 1e-12,
                    "reference_close": reference_close,
                    **metrics,
                }
            )
    return out


def aggregate_tp_group(
    rows: list[dict[str, Any]],
    *,
    group_keys: dict[str, Any],
) -> dict[str, Any]:
    n_total = len(rows)
    evaluable = [r for r in rows if r.get("evaluable") is True]
    n_eval = len(evaluable)
    hits = [r for r in evaluable if r.get("tp_hit") is True]
    ambiguous = [r for r in evaluable if r.get("same_candle_ambiguous") is True]
    ages = [int(r["first_hit_age"]) for r in hits if r.get("first_hit_age") is not None]
    mae_vals = [
        float(r["mae_before_tp_pct"])
        for r in hits
        if r.get("mae_before_tp_pct") is not None
        and not r.get("same_candle_ambiguous")
    ]
    # Also report MAE including ambiguous hits (pre-hit candles only — already computed)
    mae_all = [
        float(r["mae_before_tp_pct"])
        for r in hits
        if r.get("mae_before_tp_pct") is not None
    ]
    ages_sorted = sorted(ages)
    mae_sorted = sorted(mae_all)

    def _age_share(target: int | None) -> float | None:
        if not hits:
            return None
        if target is None:
            n = sum(1 for a in ages if a >= 4)
        else:
            n = sum(1 for a in ages if a == target)
        return n / len(hits)

    return {
        **group_keys,
        "n_total": n_total,
        "n_evaluable": n_eval,
        "n_not_evaluable": n_total - n_eval,
        "tp_hits": len(hits),
        "tp_hit_rate": (len(hits) / n_eval) if n_eval else None,
        "same_candle_ambiguous_count": len(ambiguous),
        "same_candle_ambiguous_share_of_evaluable": (
            len(ambiguous) / n_eval if n_eval else None
        ),
        "first_hit_age_median": _percentile(ages_sorted, 0.5),
        "first_hit_age_p75": _percentile(ages_sorted, 0.75),
        "mae_before_tp_median": _percentile(mae_sorted, 0.5),
        "mae_before_tp_p75": _percentile(mae_sorted, 0.75),
        "mae_before_tp_median_excluding_ambiguous": _percentile(sorted(mae_vals), 0.5),
        "hit_share_age_0": _age_share(0),
        "hit_share_age_1": _age_share(1),
        "hit_share_age_2": _age_share(2),
        "hit_share_age_3": _age_share(3),
        "hit_share_age_later": _age_share(None),
    }


def build_group_tp_summaries(
    hit_rows: list[dict[str, Any]],
    *,
    groups: Iterable[str] = GROUP_ORDER,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    tp_pct: float = PRIMARY_TP_PCT,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for group in groups:
        for horizon in horizons:
            rows = [
                r
                for r in hit_rows
                if group in (r.get("groups") or [])
                and int(r.get("horizon") or -1) == int(horizon)
                and abs(float(r.get("tp_pct") or -1) - float(tp_pct)) < 1e-12
            ]
            out.append(
                aggregate_tp_group(
                    rows,
                    group_keys={
                        "group": group,
                        "horizon": int(horizon),
                        "tp_pct": float(tp_pct),
                        "is_primary_tp": abs(float(tp_pct) - PRIMARY_TP_PCT) < 1e-12,
                    },
                )
            )
    return out


def build_threshold_comparison(
    hit_rows: list[dict[str, Any]],
    *,
    groups: Iterable[str] = GROUP_ORDER,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    thresholds: Iterable[float] = ANALYTIC_TP_THRESHOLDS,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tp in thresholds:
        out.extend(
            build_group_tp_summaries(
                hit_rows, groups=groups, horizons=horizons, tp_pct=float(tp)
            )
        )
    return out


def build_tp_audit_summary(
    *,
    signals: list[dict[str, Any]],
    hit_rows: list[dict[str, Any]],
    group_summary: list[dict[str, Any]],
    focus_horizon: int = FOCUS_HORIZON,
    primary_tp: float = PRIMARY_TP_PCT,
) -> dict[str, Any]:
    def _g(group: str) -> dict[str, Any] | None:
        for r in group_summary:
            if (
                r.get("group") == group
                and int(r.get("horizon") or -1) == focus_horizon
                and abs(float(r.get("tp_pct") or -1) - primary_tp) < 1e-12
            ):
                return r
        return None

    confirmed = _g(GROUP_MOMENTUM_CONFIRMED)
    not_conf = _g(GROUP_MOMENTUM_NOT_CONFIRMED)
    high = _g(GROUP_CONFIRMED_HIGH)
    age0 = _g(GROUP_CONFIRMED_AGE0)
    high_age0 = _g(GROUP_CONFIRMED_HIGH_AGE0)

    primary_rows = [
        r
        for r in hit_rows
        if abs(float(r.get("tp_pct") or -1) - primary_tp) < 1e-12
        and int(r.get("horizon") or -1) == focus_horizon
    ]
    ambiguous_n = sum(1 for r in primary_rows if r.get("same_candle_ambiguous"))

    answers = _answer_tp_questions(
        confirmed=confirmed,
        not_confirmed=not_conf,
        high=high,
        age0=age0,
        high_age0=high_age0,
        ambiguous_n=ambiguous_n,
        focus_horizon=focus_horizon,
        primary_tp=primary_tp,
    )
    return {
        "primary_tp_pct": primary_tp,
        "analytic_tp_thresholds": list(ANALYTIC_TP_THRESHOLDS),
        "focus_horizon_candles": focus_horizon,
        "n_signals": len(signals),
        "signal_counts": {
            "momentum_confirmed": sum(
                1 for s in signals if s.get("cohort") == COHORT_MOMENTUM_CONFIRMED
            ),
            "momentum_not_confirmed": sum(
                1
                for s in signals
                if s.get("cohort")
                in {COHORT_MOMENTUM_INVALIDATED, COHORT_MOMENTUM_EXPIRED}
            ),
            "confirmed_high": sum(
                1
                for s in signals
                if s.get("cohort") == COHORT_MOMENTUM_CONFIRMED
                and s.get("momentum_confidence") == "high"
            ),
            "confirmed_age0": sum(
                1
                for s in signals
                if s.get("cohort") == COHORT_MOMENTUM_CONFIRMED and _age_is(s, 0)
            ),
            "confirmed_high_age0": sum(
                1
                for s in signals
                if s.get("cohort") == COHORT_MOMENTUM_CONFIRMED
                and s.get("momentum_confidence") == "high"
                and _age_is(s, 0)
            ),
        },
        "focus_group_stats": {
            GROUP_MOMENTUM_CONFIRMED: confirmed,
            GROUP_MOMENTUM_NOT_CONFIRMED: not_conf,
            GROUP_CONFIRMED_HIGH: high,
            GROUP_CONFIRMED_AGE0: age0,
            GROUP_CONFIRMED_HIGH_AGE0: high_age0,
        },
        "same_candle_ambiguous_focus_primary_tp": ambiguous_n,
        "research_answers": answers,
    }


def _answer_tp_questions(
    *,
    confirmed: dict[str, Any] | None,
    not_confirmed: dict[str, Any] | None,
    high: dict[str, Any] | None,
    age0: dict[str, Any] | None,
    high_age0: dict[str, Any] | None,
    ambiguous_n: int,
    focus_horizon: int,
    primary_tp: float,
) -> dict[str, Any]:
    def rate(g: dict[str, Any] | None) -> float | None:
        if not g or g.get("tp_hit_rate") is None:
            return None
        return float(g["tp_hit_rate"])

    c_rate, n_rate = rate(confirmed), rate(not_confirmed)
    h_rate, a_rate, ha_rate = rate(high), rate(age0), rate(high_age0)

    # Age 0 vs later: compare age0 group hit rate to confirmed overall is weak;
    # prefer age0 vs (confirmed hits that aren't age0) — approximate via age0 vs confirmed
    age0_better = (
        a_rate is not None and c_rate is not None and a_rate > c_rate
    )
    high_better = h_rate is not None and c_rate is not None and h_rate > c_rate
    ha_better_than_high = (
        ha_rate is not None and h_rate is not None and ha_rate > h_rate
    )

    hits_12 = int((confirmed or {}).get("tp_hits") or 0)
    n_conf = int((confirmed or {}).get("n_evaluable") or 0)
    enough_for_entry_tp = (
        n_conf >= 20
        and hits_12 >= 8
        and (c_rate or 0) >= 0.35
        and ambiguous_n <= max(2, n_conf // 5)
    )

    return {
        "q1_confirmed_hits_0_25_within_12": {
            "hits": hits_12,
            "n_evaluable": n_conf,
            "hit_rate": c_rate,
            "answer": f"{hits_12} of {n_conf}",
        },
        "q2_not_confirmed_hit_rate": {
            "hit_rate": n_rate,
            "hits": (not_confirmed or {}).get("tp_hits"),
            "n_evaluable": (not_confirmed or {}).get("n_evaluable"),
        },
        "q3_high_better_than_all_confirmed": {
            "answer": "yes" if high_better else "no",
            "high_rate": h_rate,
            "confirmed_rate": c_rate,
        },
        "q4_age0_better_than_later_proxy": {
            "answer": "yes" if age0_better else "no",
            "note": "compares confirmed_age0 hit rate vs all confirmed",
            "age0_rate": a_rate,
            "confirmed_rate": c_rate,
        },
        "q5_high_age0_better_than_high": {
            "answer": "yes" if ha_better_than_high else "no",
            "high_age0_rate": ha_rate,
            "high_rate": h_rate,
        },
        "q6_typical_speed_to_tp": {
            "median_hit_age": (confirmed or {}).get("first_hit_age_median"),
            "p75_hit_age": (confirmed or {}).get("first_hit_age_p75"),
            "unit": "future_candles_0_based",
        },
        "q7_adverse_before_tp": {
            "median_mae_before_tp": (confirmed or {}).get("mae_before_tp_median"),
            "p75_mae_before_tp": (confirmed or {}).get("mae_before_tp_p75"),
        },
        "q8_intrabar_ambiguous": {
            "count_focus_primary": ambiguous_n,
            "confirmed_ambiguous": (confirmed or {}).get("same_candle_ambiguous_count"),
        },
        "q9_reuse_0_25_as_entry_audit_tp": {
            "answer": "yes" if enough_for_entry_tp else "cautious_yes" if (c_rate or 0) >= 0.25 else "no",
            "rationale": (
                f"primary_tp={primary_tp}%, horizon={focus_horizon}; "
                f"confirmed hit_rate={c_rate}, hits={hits_12}/{n_conf}, "
                f"ambiguous={ambiguous_n}"
            ),
        },
    }


def run_tp_hit_audit(
    *,
    price_action_confirmations: list[dict[str, Any]],
    momentum_confirmations: list[dict[str, Any]],
    momentum_events: list[dict[str, Any]],
    candles: pd.DataFrame,
    horizons: Iterable[int] = DEFAULT_HORIZONS,
    tp_thresholds: Iterable[float] = ANALYTIC_TP_THRESHOLDS,
    focus_horizon: int = FOCUS_HORIZON,
    primary_tp: float = PRIMARY_TP_PCT,
) -> dict[str, Any]:
    horizons_t = tuple(int(h) for h in horizons)
    thresholds_t = tuple(float(t) for t in tp_thresholds)
    signals = build_signal_rows(
        price_action_confirmations=price_action_confirmations,
        momentum_confirmations=momentum_confirmations,
        momentum_events=momentum_events,
    )
    _, ts_to_i, candle_rows = _candle_maps(candles)

    hit_rows: list[dict[str, Any]] = []
    for signal in signals:
        hit_rows.extend(
            evaluate_signal_tp_hits(
                signal,
                candles=candle_rows,
                ts_to_i=ts_to_i,
                horizons=horizons_t,
                tp_thresholds=thresholds_t,
            )
        )

    # Flatten groups for CSV: one row per (signal, group, horizon, tp)
    flat_rows: list[dict[str, Any]] = []
    for r in hit_rows:
        groups = list(r.get("groups") or [])
        if not groups:
            groups = ["ungrouped"]
        for g in groups:
            flat = dict(r)
            flat["group"] = g
            flat.pop("groups", None)
            flat_rows.append(flat)

    group_summary = build_group_tp_summaries(
        hit_rows, horizons=horizons_t, tp_pct=primary_tp
    )
    threshold_comparison = build_threshold_comparison(
        hit_rows, horizons=horizons_t, thresholds=thresholds_t
    )
    audit_summary = build_tp_audit_summary(
        signals=signals,
        hit_rows=hit_rows,
        group_summary=group_summary,
        focus_horizon=focus_horizon,
        primary_tp=primary_tp,
    )
    return {
        "signals": signals,
        "signal_tp_hits": flat_rows,
        "group_tp_summary": group_summary,
        "tp_threshold_comparison": threshold_comparison,
        "audit_summary": audit_summary,
    }


def format_tp_readme(audit_summary: dict[str, Any]) -> str:
    a = audit_summary.get("research_answers") or {}
    focus = audit_summary.get("focus_horizon_candles")
    primary = audit_summary.get("primary_tp_pct")
    stats = audit_summary.get("focus_group_stats") or {}

    def _fmt_rate(g: dict[str, Any] | None) -> str:
        if not g or g.get("tp_hit_rate") is None:
            return "n/a"
        return (
            f"{100.0 * float(g['tp_hit_rate']):.1f}% "
            f"({g.get('tp_hits')}/{g.get('n_evaluable')})"
        )

    lines = [
        "# Momentum TP-Hit Audit (March week)",
        "",
        f"Primary TP: **{primary}%**. Analytic thresholds: "
        f"`{audit_summary.get('analytic_tp_thresholds')}`. No SL / live changes.",
        f"Focus horizon: **{focus}** candles after measurement.",
        "",
        f"Signal counts: `{audit_summary.get('signal_counts')}`",
        "",
        "## Research answers",
        "",
        "### 1. Wie viele der confirmed Signale erreichen 0,25 % innerhalb von 12 Candles?",
        f"- {a.get('q1_confirmed_hits_0_25_within_12')}",
        "",
        "### 2. Trefferquote not_confirmed?",
        f"- {_fmt_rate(stats.get(GROUP_MOMENTUM_NOT_CONFIRMED))} — {a.get('q2_not_confirmed_hit_rate')}",
        "",
        "### 3. Ist `high` besser als alle confirmed?",
        f"- **{(a.get('q3_high_better_than_all_confirmed') or {}).get('answer')}** — "
        f"{a.get('q3_high_better_than_all_confirmed')}",
        "",
        "### 4. Ist Age 0 besser als spätere Bestätigung?",
        f"- **{(a.get('q4_age0_better_than_later_proxy') or {}).get('answer')}** — "
        f"{a.get('q4_age0_better_than_later_proxy')}",
        "",
        "### 5. Ist high + Age 0 besser als high allein?",
        f"- **{(a.get('q5_high_age0_better_than_high') or {}).get('answer')}** — "
        f"{a.get('q5_high_age0_better_than_high')}",
        "",
        "### 6. Wie schnell wird der TP typischerweise erreicht?",
        f"- {a.get('q6_typical_speed_to_tp')} (0-based future candle index)",
        "",
        "### 7. Wie groß ist die Gegenbewegung vor dem TP?",
        f"- {a.get('q7_adverse_before_tp')}",
        "",
        "### 8. Wie viele Fälle sind intrabar unklar?",
        f"- {a.get('q8_intrabar_ambiguous')}",
        "",
        "### 9. 0,25 % als ersten Entry-Audit-TP weiterverwenden?",
        f"- **{(a.get('q9_reuse_0_25_as_entry_audit_tp') or {}).get('answer')}** — "
        f"{a.get('q9_reuse_0_25_as_entry_audit_tp')}",
        "",
        "## Focus rates (primary TP)",
        "",
        f"- confirmed: {_fmt_rate(stats.get(GROUP_MOMENTUM_CONFIRMED))}",
        f"- not_confirmed: {_fmt_rate(stats.get(GROUP_MOMENTUM_NOT_CONFIRMED))}",
        f"- confirmed_high: {_fmt_rate(stats.get(GROUP_CONFIRMED_HIGH))}",
        f"- confirmed_age0: {_fmt_rate(stats.get(GROUP_CONFIRMED_AGE0))}",
        f"- confirmed_high_age0: {_fmt_rate(stats.get(GROUP_CONFIRMED_HIGH_AGE0))}",
        "",
        "## Method notes",
        "",
        "- Measurement bases unchanged from forward audit.",
        "- Hit age 0 = first candle **after** the measurement candle.",
        "- Exact TP threshold counts as hit (`fav >= tp`).",
        "- Same-candle favorable+adverse → `same_candle_ambiguous`; MAE-before-TP "
        "still excludes that candle's adverse leg.",
        "- Long/short mirrored via directional excursions.",
        "",
    ]
    return "\n".join(lines)


def write_tp_hit_outputs(
    payload: dict[str, Any],
    output_dir: str | Path,
) -> dict[str, Path]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {
        "hits_csv": out / "signal_tp_hits.csv",
        "group_csv": out / "group_tp_summary.csv",
        "threshold_csv": out / "tp_threshold_comparison.csv",
        "summary_json": out / "audit_summary.json",
        "readme": out / "README.md",
    }
    pd.DataFrame(json_safe(payload.get("signal_tp_hits") or [])).to_csv(
        paths["hits_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("group_tp_summary") or [])).to_csv(
        paths["group_csv"], index=False
    )
    pd.DataFrame(json_safe(payload.get("tp_threshold_comparison") or [])).to_csv(
        paths["threshold_csv"], index=False
    )
    paths["summary_json"].write_text(
        json.dumps(json_safe(payload.get("audit_summary") or {}), indent=2),
        encoding="utf-8",
    )
    paths["readme"].write_text(
        format_tp_readme(payload.get("audit_summary") or {}), encoding="utf-8"
    )
    return paths


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Momentum TP-hit audit (research-only).")
    p.add_argument(
        "--pipeline-dir",
        default=(
            "research/backtests/results/regime_scanner_pipeline_audit_march_week1_r4_momentum"
        ),
    )
    p.add_argument(
        "--output-dir",
        default="research/backtests/results/regime_scanner_momentum_tp_hit_audit_march_week1",
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--start", default="2026-03-01")
    p.add_argument("--end", default="2026-03-08")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    arts = load_pipeline_artifacts(args.pipeline_dir)
    raw = load_symbol_candles(args.symbol)
    prepared = prepare_candle_window(
        raw,
        start=args.start,
        end=args.end,
        history_candles=144,
        timeframes="5m,15m,30m",
    )
    payload = run_tp_hit_audit(
        price_action_confirmations=arts["price_action_confirmations"],
        momentum_confirmations=arts["momentum_confirmations"],
        momentum_events=arts["momentum_events"],
        candles=prepared["candles"],
    )
    paths = write_tp_hit_outputs(payload, args.output_dir)
    summary = payload["audit_summary"]
    print(
        f"TP-hit audit: counts={summary.get('signal_counts')} "
        f"answers={summary.get('research_answers')}"
    )
    for path in paths.values():
        print(f"Wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
