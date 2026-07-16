"""Phase C3 / C3.1: trend / pullback / range / transition classification audit.

Compares regime classifiers against frozen C2 baseline without mutating
production config or baseline artifacts. C3.1 adds range calibration metrics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_audit_shared_replay import (
    load_or_build_shared_context,
    reset_audit_counters,
    step_trend_state_from_prepared,
)
import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod
from research.regime_scanner.trend_pine_export import (
    build_c2_c3_comparison_payload,
    export_c3_pine_bundle,
)
from research.regime_scanner.trend_regime_classifier import (
    VARIANT_ALIASES,
    c2_direction,
    c3_direction,
    config_c3,
    precompute_regime_arrays,
    replay_regime_variant,
)
from research.regime_scanner.trend_robustness_audit import (
    ANALYZE_END,
    ANALYZE_START,
    LOAD_END,
    LOAD_START,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_forward_outcome_audit import (
    FAST_REVERSAL_WINDOWS,
    build_price_arrays,
    compute_horizon_outcome,
    fast_reversal_flags,
    state_duration_until_next_change,
)
from research.regime_scanner.trend_state_machine import TrendRuntime, trend_state_config_c1
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_1_range_calibration")
DEFAULT_BASELINE = Path(
    "research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3"
)
C3_PRIOR_SMOKE = Path("research/regime_scanner/results/phase_c3_mar_smoke")
DEFAULT_HORIZONS: tuple[int, ...] = (3, 6, 12, 24, 48)
C2_BASELINE_HASH = "702ba3e62976aeae879d053a03f64eaba06771beac367248dcfca8d4ebc4ec61"
MIN_EVENTS = 5
C31_DEFAULT_VARIANTS = ["conservative", "balanced"]

C3_EXPECTED_SIDE: dict[str, str | None] = {
    "confirmed_uptrend": "long",
    "confirmed_downtrend": "short",
    "range_sideways": None,
    "bullish_pullback": None,
    "bearish_pullback": None,
    "transition_up": None,
    "transition_down": None,
    "unclear": None,
}


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def resolve_variants(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        key = n.strip().lower()
        if key not in VARIANT_ALIASES:
            raise ValueError(f"unknown variant {n!r}")
        out.append(VARIANT_ALIASES[key])
    return out


def replay_c2_loose_timeline(
    shared,
    *,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
) -> list[dict[str, Any]]:
    cfg = trend_state_config_c1("loose")
    rt = TrendRuntime()
    rows: list[dict[str, Any]] = []
    for prep in shared.prepared_bars:
        ts = prep.decision_time
        if ts < analyze_start or ts > analyze_end:
            continue
        prev = rt.state
        rt, snap, _ = step_trend_state_from_prepared(rt, prepared=prep, cfg=cfg)
        rows.append(
            {
                "decision_time": ts.isoformat(),
                "bar_index": int(prep.bar_index),
                "c2_state": snap.current_state,
                "previous_c2_state": prev,
                "close": float(prep.row.get("close", 0.0)),
            }
        )
    return rows


def _load_baseline_summary_hash(baseline_dir: Path) -> str:
    summary_path = baseline_dir / "summary.json"
    if not summary_path.is_file():
        return ""
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    return str(data.get("deterministic_hash") or "")


def assert_baseline_readonly(baseline_dir: Path) -> dict[str, Any]:
    baseline_dir = baseline_dir.resolve()
    before_hash = _load_baseline_summary_hash(baseline_dir)
    sums_path = baseline_dir / "SHA256SUMS.txt"
    return {
        "baseline_dir": str(baseline_dir),
        "baseline_hash": before_hash,
        "expected_hash": C2_BASELINE_HASH,
        "hash_matches": before_hash == C2_BASELINE_HASH,
        "sha256sums_present": sums_path.is_file(),
    }


def _median(vals: list[float]) -> float | None:
    return float(statistics.median(vals)) if vals else None


def _mean(vals: list[float]) -> float | None:
    return float(sum(vals) / len(vals)) if vals else None


def _state_runs(timeline: list[dict[str, Any]], state_key: str = "state") -> list[dict[str, Any]]:
    if not timeline:
        return []
    runs: list[dict[str, Any]] = []
    cur = str(timeline[0][state_key])
    start = 0
    for i in range(1, len(timeline)):
        st = str(timeline[i][state_key])
        if st != cur:
            runs.append({"state": cur, "length": i - start, "start_index": start})
            cur = st
            start = i
    runs.append({"state": cur, "length": len(timeline) - start, "start_index": start})
    return runs


def enrich_timeline_outcomes(
    timeline: list[dict[str, Any]],
    *,
    arrays: dict[str, Any],
    horizons: tuple[int, ...],
    state_key: str = "state",
) -> list[dict[str, Any]]:
    state_by_bar = ["unclear"] * int(arrays["n_bars"])
    for row in timeline:
        state_by_bar[int(row["bar_index"])] = str(row[state_key])

    enriched: list[dict[str, Any]] = []
    for row in timeline:
        bar_i = int(row["bar_index"])
        st = str(row[state_key])
        ref = float(row["close"])
        side = C3_EXPECTED_SIDE.get(st)
        out = dict(row)
        out["state_duration_bars"] = state_duration_until_next_change(
            bar_i, st, state_by_bar
        )
        out.update(
            fast_reversal_flags(
                event_bar=bar_i,
                previous_state=str(row.get("previous_state") or row.get("previous_c2_state") or ""),
                new_state=st,
                state_by_bar=state_by_bar,
            )
        )
        for h in horizons:
            metrics = compute_horizon_outcome(
                bar_index=bar_i,
                horizon=int(h),
                reference_close=ref,
                side=side,
                arrays=arrays,
            )
            for k, v in metrics.items():
                if k == "horizon":
                    continue
                out[f"h{h}_{k}"] = v
        enriched.append(out)
    return enriched


def aggregate_variant(
    timeline: list[dict[str, Any]],
    *,
    variant: str,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    n_bars = len(timeline)
    runs = _state_runs(timeline)
    by_state: dict[str, list[dict[str, Any]]] = {}
    for row in timeline:
        by_state.setdefault(str(row["state"]), []).append(row)

    state_metrics: list[dict[str, Any]] = []
    for state, rows in sorted(by_state.items()):
        lengths = [int(r["state_duration_bars"]) for r in rows if r.get("state_duration_bars")]
        fast3 = [1.0 if r.get("fast_reversal_within_3") else 0.0 for r in rows]
        for h in horizons:
            evaluable = [r for r in rows if r.get(f"h{h}_evaluable")]
            hits = [
                r[f"h{h}_direction_hit"]
                for r in evaluable
                if r.get(f"h{h}_direction_hit") is not None
            ]
            drets = [
                float(r[f"h{h}_directional_close_return_pct"])
                for r in evaluable
                if r.get(f"h{h}_directional_close_return_pct") is not None
            ]
            raws = [
                float(r[f"h{h}_raw_close_return_pct"])
                for r in evaluable
                if r.get(f"h{h}_raw_close_return_pct") is not None
            ]
            mfes = [float(r[f"h{h}_mfe_pct"]) for r in evaluable if r.get(f"h{h}_mfe_pct") is not None]
            maes = [float(r[f"h{h}_mae_pct"]) for r in evaluable if r.get(f"h{h}_mae_pct") is not None]
            state_metrics.append(
                {
                    "variant": variant,
                    "state": state,
                    "horizon": h,
                    "n_bars": len(rows),
                    "time_share": len(rows) / max(1, n_bars),
                    "n_runs": sum(1 for r in runs if r["state"] == state),
                    "median_run_duration": _median(
                        [float(r["length"]) for r in runs if r["state"] == state]
                    ),
                    "max_run_duration": max(
                        [int(r["length"]) for r in runs if r["state"] == state], default=0
                    ),
                    "n_evaluable": len(evaluable),
                    "hit_rate": (sum(1 for x in hits if x) / len(hits)) if hits else None,
                    "mean_directional_return": _mean(drets),
                    "median_directional_return": _median(drets),
                    "mean_raw_return": _mean(raws),
                    "median_raw_return": _median(raws),
                    "mean_mfe": _mean(mfes),
                    "median_mfe": _median(mfes),
                    "mean_mae": _mean(maes),
                    "median_mae": _median(maes),
                    "mean_mfe_mae_ratio": _mean(
                        [
                            float(r[f"h{h}_mfe_mae_ratio"])
                            for r in evaluable
                            if r.get(f"h{h}_mfe_mae_ratio") is not None
                        ]
                    ),
                    "fast_reversal_rate_within_3": _mean(fast3),
                }
            )

    range_metrics = compute_range_audit_metrics(timeline, runs)
    return {
        "variant": variant,
        "n_bars": n_bars,
        "n_transitions": sum(1 for r in timeline if r.get("transition")),
        "state_counts": {s: len(v) for s, v in by_state.items()},
        "aggregate_by_state_horizon": state_metrics,
        "range_metrics": range_metrics,
    }


def compute_range_audit_metrics(
    timeline: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    n_bars = max(1, len(timeline))
    range_rows = [r for r in timeline if r.get("state") == "range_sideways"]
    range_runs = [r for r in runs if r["state"] == "range_sideways"]
    scores = [float(r["range_score"]) for r in range_rows if r.get("range_score") is not None]
    widths = [
        float(r["range_width_atr"])
        for r in range_rows
        if r.get("range_width_atr") is not None and float(r.get("range_width_atr") or 0) > 0
    ]
    failed_bo = sum(1 for r in timeline if r.get("failed_breakout_event"))
    pullback_to_range = sum(
        1
        for r in timeline
        if r.get("transition")
        and str(r.get("previous_state", "")).endswith("pullback")
        and r.get("state") == "range_sideways"
    )
    range_to_parent = 0
    range_to_opposite = 0
    false_breakouts = failed_bo
    successful_breakouts = 0
    breakout_dirs: list[str] = []
    bars_exit_to_confirm: list[float] = []

    for i, row in enumerate(timeline):
        if not row.get("transition"):
            continue
        prev = str(row.get("previous_state") or "")
        new = str(row.get("state") or "")
        reasons = str(row.get("reasons") or "")
        if prev == "range_sideways" and new in {"transition_up", "transition_down"}:
            successful_breakouts += 1
            direction = "up" if new == "transition_up" else "down"
            breakout_dirs.append(direction)
            # distance to confirmed trend
            for j in range(i + 1, len(timeline)):
                st = str(timeline[j].get("state") or "")
                if st in {"confirmed_uptrend", "confirmed_downtrend"}:
                    bars_exit_to_confirm.append(float(j - i))
                    parent = row.get("parent_trend")
                    if parent == "up" and st == "confirmed_uptrend":
                        range_to_parent += 1
                    elif parent == "down" and st == "confirmed_downtrend":
                        range_to_parent += 1
                    elif parent == "up" and st == "confirmed_downtrend":
                        range_to_opposite += 1
                    elif parent == "down" and st == "confirmed_uptrend":
                        range_to_opposite += 1
                    elif parent in {None, "none"}:
                        if direction == "up" and st == "confirmed_uptrend":
                            range_to_opposite += 0
                        # count opposite relative to prior parent if any
                        pass
                    break
                if st == "range_sideways":
                    break
        if "pullback_to_range" in reasons:
            pullback_to_range += 1

    # Precision proxy: share of range bars with |raw H12| below median abs return of all bars
    h12_abs = [
        abs(float(r["h12_raw_close_return_pct"]))
        for r in timeline
        if r.get("h12_raw_close_return_pct") is not None
    ]
    median_abs = _median(h12_abs) or 0.0
    quiet_range = [
        r
        for r in range_rows
        if r.get("h12_raw_close_return_pct") is not None
        and abs(float(r["h12_raw_close_return_pct"])) <= median_abs
    ]
    range_precision_proxy = (
        len(quiet_range) / len(range_rows) if range_rows else None
    )

    return {
        "percent_range_bars": len(range_rows) / n_bars,
        "n_range_bars": len(range_rows),
        "n_range_runs": len(range_runs),
        "range_duration_distribution": {
            "median": _median([float(r["length"]) for r in range_runs]),
            "max": max([int(r["length"]) for r in range_runs], default=0),
            "mean": _mean([float(r["length"]) for r in range_runs]),
        },
        "median_range_score": _median(scores),
        "median_range_width_atr": _median(widths),
        "range_precision_proxy": range_precision_proxy,
        "false_range_breakouts": false_breakouts,
        "successful_range_breakouts": successful_breakouts,
        "breakout_direction_counts": {
            "up": sum(1 for d in breakout_dirs if d == "up"),
            "down": sum(1 for d in breakout_dirs if d == "down"),
        },
        "bars_from_range_exit_to_confirmed_trend": {
            "median": _median(bars_exit_to_confirm),
            "mean": _mean(bars_exit_to_confirm),
            "n": len(bars_exit_to_confirm),
        },
        "pullback_to_range_count": pullback_to_range,
        "range_to_parent_trend_count": range_to_parent,
        "range_to_opposite_trend_count": range_to_opposite,
    }


def build_visual_review_cases(
    c2_timeline: list[dict[str, Any]],
    c3_timeline: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    cases = [
        (
            "VR01_early_uptrend",
            "2026-03-02T00:00:00+00:00",
            "2026-03-03T23:55:00+00:00",
            "avoid_early_uptrend",
            None,
            "placeholder",
        ),
        (
            "VR02_sideways_mar3_6",
            "2026-03-03T00:00:00+00:00",
            "2026-03-06T23:55:00+00:00",
            "range_sideways",
            "transition_down -> confirmed_downtrend",
            "manually marked horizontal range in TradingView",
        ),
        (
            "VR03_downtrend_mar8",
            "2026-03-08T00:00:00+00:00",
            "2026-03-08T23:55:00+00:00",
            "confirmed_downtrend",
            None,
            "placeholder",
        ),
        (
            "VR04_bull_structure_after",
            "2026-03-09T00:00:00+00:00",
            "2026-03-10T23:55:00+00:00",
            "transition_or_pullback",
            None,
            "placeholder",
        ),
        (
            "VR05_downtrend_mar11",
            "2026-03-11T00:00:00+00:00",
            "2026-03-11T23:55:00+00:00",
            "confirmed_downtrend",
            None,
            "placeholder",
        ),
    ]
    out: list[dict[str, Any]] = []
    for case_id, start, end, expected, expected_exit, notes in cases:
        s, e = _ts(start), _ts(end)
        c2_modes: dict[str, int] = {}
        c3_modes: dict[str, int] = {}
        c3_seq: list[str] = []
        for row in c2_timeline:
            ts = _ts(row["decision_time"])
            if s <= ts <= e:
                c2_modes[row["c2_state"]] = c2_modes.get(row["c2_state"], 0) + 1
        for row in c3_timeline:
            ts = _ts(row["decision_time"])
            if s <= ts <= e:
                st = str(row["state"])
                c3_modes[st] = c3_modes.get(st, 0) + 1
                if not c3_seq or c3_seq[-1] != st:
                    c3_seq.append(st)
        # Extend VR02 window slightly for exit sequence observation
        exit_end = _ts("2026-03-08T23:55:00+00:00") if case_id.startswith("VR02") else e
        exit_seq: list[str] = []
        for row in c3_timeline:
            ts = _ts(row["decision_time"])
            if e < ts <= exit_end:
                st = str(row["state"])
                if not exit_seq or exit_seq[-1] != st:
                    exit_seq.append(st)
        out.append(
            {
                "case_id": case_id,
                "start_time": start,
                "end_time": end,
                "expected_context": expected,
                "expected_exit": expected_exit,
                "notes": notes,
                "c2_classification": max(c2_modes, key=c2_modes.get) if c2_modes else None,
                "c3_classification": max(c3_modes, key=c3_modes.get) if c3_modes else None,
                "c3_state_sequence": " -> ".join(c3_seq[:12]),
                "post_window_sequence": " -> ".join(exit_seq[:8]),
                "range_bar_share": (
                    c3_modes.get("range_sideways", 0) / max(1, sum(c3_modes.values()))
                    if c3_modes
                    else None
                ),
                "review_status": "manual_review_anchor"
                if case_id.startswith("VR02")
                else "placeholder_manual_review",
            }
        )
    return out


def recommend_variant(aggregates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Recommend among conservative/balanced only for C3.1; never auto-prefer responsive."""
    eligible = {
        v: a
        for v, a in aggregates.items()
        if v in {"C3_A_conservative", "C3_B_balanced"}
    }
    if not eligible:
        eligible = {
            v: a
            for v, a in aggregates.items()
            if v != "C3_C_responsive"
        }
    scores: dict[str, float] = {v: 0.0 for v in eligible}
    for variant, agg in eligible.items():
        metrics = agg.get("aggregate_by_state_horizon") or []
        rng_m = agg.get("range_metrics") or {}
        up = next((m for m in metrics if m["state"] == "confirmed_uptrend" and m["horizon"] == 12), None)
        down = next(
            (m for m in metrics if m["state"] == "confirmed_downtrend" and m["horizon"] == 12), None
        )
        rng = next((m for m in metrics if m["state"] == "range_sideways" and m["horizon"] == 12), None)
        if up and up.get("n_evaluable", 0) >= MIN_EVENTS and up.get("hit_rate") is not None:
            scores[variant] += float(up["hit_rate"]) * 2.0
        if down and down.get("n_evaluable", 0) >= MIN_EVENTS and down.get("hit_rate") is not None:
            scores[variant] += float(down["hit_rate"]) * 2.0
        # Prefer meaningful but not dominant range share (VR02-like)
        pct = float(rng_m.get("percent_range_bars") or 0.0)
        if 0.08 <= pct <= 0.40:
            scores[variant] += 0.8
        elif pct > 0.50:
            scores[variant] -= 0.8
        elif pct < 0.02:
            scores[variant] -= 0.3
        precision = rng_m.get("range_precision_proxy")
        if precision is not None:
            scores[variant] += float(precision) * 0.8
        if rng and rng.get("n_evaluable", 0) >= MIN_EVENTS:
            scores[variant] += max(0.0, 0.05 - abs(float(rng.get("mean_raw_return") or 0.0))) * 5.0
        scores[variant] -= float(agg.get("n_transitions", 0)) / max(1.0, float(agg.get("n_bars", 1))) * 2.0

    if not scores or max(scores.values()) < 0.5:
        choice = "inconclusive"
    else:
        choice_key = max(scores, key=scores.get)
        choice = {
            "C3_A_conservative": "conservative",
            "C3_B_balanced": "balanced",
        }.get(choice_key, "inconclusive")
        if choice_key == "C3_C_responsive":
            choice = "inconclusive"
    return {
        "recommendation": choice,
        "scores": scores,
        "responsive_excluded_from_c31": True,
        "production_default_remains": "off (C1-A baseline)",
    }


def build_baseline_comparison(
    c2_timeline: list[dict[str, Any]],
    c3_timeline: list[dict[str, Any]],
    *,
    c3_variant: str,
    arrays: dict[str, Any],
    horizons: tuple[int, ...],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    c3_by_time = {r["decision_time"]: r for r in c3_timeline}
    mapping_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    transition_rows: list[dict[str, Any]] = []

    for c2 in c2_timeline:
        ts = c2["decision_time"]
        c3 = c3_by_time.get(ts)
        if c3 is None:
            continue
        c2_st = str(c2["c2_state"])
        c3_st = str(c3["state"])
        c2_dir = c2_direction(c2_st)
        c3_dir = c3_direction(c3_st)
        agree = c2_dir == c3_dir or (
            c2_dir in {"transition_up", "transition_down"}
            and c3_dir in {"transition_up", "transition_down", "pullback_up", "pullback_down"}
        )
        improvement = "neutral"
        if c2_dir in {"up", "down"} and c3_dir == "range":
            improvement = "c3_downgrade_to_range"
        elif c2_dir in {"up", "down"} and c3_dir.startswith("pullback"):
            improvement = "c3_pullback_not_flip"
        elif c2_dir != c3_dir and c3_dir in {"up", "down"}:
            improvement = "direction_change"

        h12_raw = None
        bar_i = int(c3["bar_index"])
        m = compute_horizon_outcome(
            bar_index=bar_i,
            horizon=12,
            reference_close=float(c3["close"]),
            side=C3_EXPECTED_SIDE.get(c3_st),
            arrays=arrays,
        )
        if m.get("evaluable"):
            h12_raw = m.get("raw_close_return_pct")

        row = {
            "decision_time": ts,
            "close": float(c3["close"]),
            "c2_state": c2_st,
            "c3_state": c3_st,
            "c2_direction": c2_dir,
            "c3_direction": c3_dir,
            "c3_parent_trend": c3.get("parent_trend"),
            "c3_in_range": c3.get("in_range"),
            "agreement": bool(agree),
            "improvement_flag": improvement,
            "h12_raw_return_pct": h12_raw,
            "c3_variant": c3_variant,
        }
        mapping_rows.append(row)
        comparison_rows.append(row)
        if c2_st != c3_st:
            transition_rows.append(row)

    return mapping_rows, comparison_rows, transition_rows


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def _flatten_range_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for k, v in metrics.items():
        if isinstance(v, dict):
            for sk, sv in v.items():
                flat[f"{k}_{sk}"] = sv
        else:
            flat[k] = v
    return flat


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    variants: list[str] | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    export_pine: bool = True,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_info = assert_baseline_readonly(baseline_dir)

    t0 = time.perf_counter()
    reset_audit_counters()
    variant_names = resolve_variants(variants or list(C31_DEFAULT_VARIANTS))
    a0, a1 = _ts(analyze_start), _ts(analyze_end)

    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    shared = load_or_build_shared_context(frame, cache_dir=output_dir / ".cache")
    t_shared = time.perf_counter()

    cfg0 = config_c3("balanced")
    regime_arrays = precompute_regime_arrays(
        frame,
        efficiency_window=cfg0.efficiency_window,
        net_move_window=cfg0.net_move_window,
        overlap_window=cfg0.overlap_window,
        range_width_window=cfg0.range_width_window,
        range_lookback=cfg0.range_lookback,
        failed_breakout_window=cfg0.failed_breakout_window,
        alternating_window=cfg0.alternating_window,
    )
    price_arrays = build_price_arrays(frame)

    c2_timeline = replay_c2_loose_timeline(shared, analyze_start=a0, analyze_end=a1)
    t_c2 = time.perf_counter()

    variant_results: dict[str, Any] = {}
    all_states: list[dict[str, Any]] = []
    all_transitions: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []
    aggregates: dict[str, Any] = {}
    mapping_all: list[dict[str, Any]] = []
    comparison_all: list[dict[str, Any]] = []
    transition_all: list[dict[str, Any]] = []

    for vname in variant_names:
        key = {
            "C3_A_conservative": "conservative",
            "C3_B_balanced": "balanced",
            "C3_C_responsive": "responsive",
        }[vname]
        cfg = config_c3(key)
        replay = replay_regime_variant(
            shared.prepared_bars,
            arrays=regime_arrays,
            cfg=cfg,
            analyze_start=a0,
            analyze_end=a1,
        )
        timeline = enrich_timeline_outcomes(
            replay["timeline"], arrays=price_arrays, horizons=horizons
        )
        agg = aggregate_variant(timeline, variant=vname, horizons=horizons)
        variant_results[vname] = {"replay": replay, "timeline": timeline, "aggregate": agg}
        aggregates[vname] = agg
        all_states.extend(timeline)
        all_transitions.extend(replay["transitions"])
        for tr in replay["transitions"]:
            all_events.append({**tr, "variant": vname, "event_type": "state_transition"})
        map_rows, cmp_rows, tr_rows = build_baseline_comparison(
            c2_timeline, timeline, c3_variant=vname, arrays=price_arrays, horizons=horizons
        )
        for r in map_rows:
            r["variant"] = vname
        mapping_all.extend(map_rows)
        comparison_all.extend(cmp_rows)
        transition_all.extend(tr_rows)

    t_replay = time.perf_counter()

    # Use balanced for visual review placeholders
    primary_c3 = variant_results.get("C3_B_balanced", next(iter(variant_results.values())))["timeline"]
    visual_cases = build_visual_review_cases(c2_timeline, primary_c3)
    recommendation = recommend_variant(aggregates)

    write_csv(output_dir / "states.csv", all_states)
    write_csv(output_dir / "transitions.csv", all_transitions)
    write_csv(output_dir / "events.csv", all_events)
    write_csv(
        output_dir / "aggregate_by_state_horizon.csv",
        [m for a in aggregates.values() for m in a["aggregate_by_state_horizon"]],
    )
    write_csv(output_dir / "state_mapping_comparison.csv", mapping_all)
    write_csv(output_dir / "baseline_comparison.csv", comparison_all)
    write_csv(output_dir / "transition_comparison.csv", transition_all)
    write_csv(output_dir / "visual_review_cases.csv", visual_cases)
    write_csv(
        output_dir / "range_metrics.csv",
        [
            {"variant": v, **_flatten_range_metrics(aggregates[v].get("range_metrics") or {})}
            for v in variant_names
        ],
    )

    variant_comparison = [
        {
            "variant": v,
            "n_bars": aggregates[v]["n_bars"],
            "n_transitions": aggregates[v]["n_transitions"],
            "percent_range_bars": (aggregates[v].get("range_metrics") or {}).get(
                "percent_range_bars"
            ),
            "median_range_score": (aggregates[v].get("range_metrics") or {}).get(
                "median_range_score"
            ),
            "false_range_breakouts": (aggregates[v].get("range_metrics") or {}).get(
                "false_range_breakouts"
            ),
            "successful_range_breakouts": (aggregates[v].get("range_metrics") or {}).get(
                "successful_range_breakouts"
            ),
            **aggregates[v]["state_counts"],
        }
        for v in variant_names
    ]
    write_csv(output_dir / "variant_comparison.csv", variant_comparison)

    primary_variant = {
        "conservative": "C3_A_conservative",
        "balanced": "C3_B_balanced",
    }.get(str(recommendation.get("recommendation")), "C3_B_balanced")
    if primary_variant not in variant_results:
        primary_variant = next(iter(variant_results))
    primary_timeline = variant_results[primary_variant]["timeline"]
    comparison_payload = build_c2_c3_comparison_payload(c2_timeline, primary_timeline)

    summary_core = {
        "phase": "C3_1_range_calibration",
        "symbol": symbol,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "horizons": list(horizons),
        "baseline": baseline_info,
        "variants": {v: aggregates[v] for v in variant_names},
        "variant_comparison": variant_comparison,
        "recommendation": recommendation,
        "c2_baseline_reference_hash": C2_BASELINE_HASH,
        "c3_prior_smoke_dir": str(C3_PRIOR_SMOKE),
    }
    blob = json.dumps(json_safe(summary_core), sort_keys=True, separators=(",", ":"))
    det_hash = hashlib.sha256(blob.encode()).hexdigest()

    pine_meta = None
    t_pine = t_replay
    if export_pine:
        pine_payload = {
            v: {
                "timeline_rows": [
                    {
                        "decision_time": r["decision_time"],
                        "state": r["state"],
                        "previous_state": r.get("previous_state"),
                        "close": r["close"],
                        "transition": r.get("transition"),
                        "reasons": r.get("reasons", ""),
                        "range_score": r.get("range_score"),
                        "range_confirmed": r.get("range_confirmed"),
                        "range_high": r.get("range_high"),
                        "range_low": r.get("range_low"),
                        "bars_in_range": r.get("bars_in_range"),
                        "failed_breakout_event": r.get("failed_breakout_event"),
                    }
                    for r in variant_results[v]["timeline"]
                ],
                "marker_rows": [
                    {
                        "decision_time": t["decision_time"],
                        "new_state": t["new_state"],
                        "label": f"{t['previous_state']}->{t['new_state']}",
                    }
                    for t in variant_results[v]["replay"]["transitions"]
                ],
            }
            for v in variant_names
        }
        pine_meta = export_c3_pine_bundle(
            output_dir=output_dir,
            symbol=symbol,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            audit_hash=det_hash,
            variants=pine_payload,
            recommended_variant=primary_variant,
            comparison=comparison_payload,
        )
        t_pine = time.perf_counter()

    metadata = {
        **summary_core,
        "variant_configs": {
            v: config_c3(
                {"C3_A_conservative": "conservative", "C3_B_balanced": "balanced", "C3_C_responsive": "responsive"}[v]
            ).to_dict()
            for v in variant_names
        },
        "performance": {
            "elapsed_seconds": round(time.perf_counter() - t0, 3),
            "shared_pass_seconds": round(t_shared - t0, 3),
            "c2_replay_seconds": round(t_c2 - t_shared, 3),
            "c3_replay_seconds": round(t_replay - t_c2, 3),
            "pine_export_seconds": round(t_pine - t_replay, 3),
            "shared_structure_passes": shared.structure_pass_count,
            "variant_policy_replays": shared_replay_mod.VARIANT_POLICY_REPLAY_COUNT,
        },
        "pine_export": pine_meta,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(json_safe(metadata), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (output_dir / "baseline_comparison.json").write_text(
        json.dumps(json_safe({"rows": comparison_all, "baseline": baseline_info}), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = {**summary_core, "deterministic_hash": det_hash, "performance": metadata["performance"]}
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    after_baseline_hash = _load_baseline_summary_hash(baseline_dir)
    if after_baseline_hash != baseline_info["baseline_hash"]:
        raise RuntimeError("baseline directory was modified during audit")

    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase C3.1 range calibration audit")
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE)
    p.add_argument("--load-start", default=LOAD_START)
    p.add_argument("--load-end", default=LOAD_END)
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    p.add_argument("--variants", nargs="+", default=list(C31_DEFAULT_VARIANTS))
    p.add_argument("--horizons", nargs="+", type=int, default=list(DEFAULT_HORIZONS))
    p.add_argument("--export-pine", action=argparse.BooleanOptionalAction, default=True)
    args = p.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        variants=args.variants,
        horizons=tuple(int(h) for h in args.horizons),
        export_pine=bool(args.export_pine),
    )
    print(json.dumps({"hash": summary["deterministic_hash"], "recommendation": summary["recommendation"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
