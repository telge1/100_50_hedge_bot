"""Phase C3.2B-D indicator-pattern audit orchestrator.

This module layers the C3.2 indicator-pattern replay on top of the existing C3.1
regime replay, writes research-only artifacts, and exports Pine review bundles.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import numpy as np
import pandas as pd

from research.regime_scanner.indicator_feature_store import (
    INDICATOR_FEATURE_VERSION,
    load_or_build_indicator_features,
)
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.trend_audit_shared_replay import (
    attach_c32a_indicator_features,
    load_or_build_shared_context,
)
from research.regime_scanner.trend_indicator_ablation import (
    ALL_VARIANTS,
    VARIANT_BASELINE,
    VARIANT_EMA,
    VARIANT_EMA_ADX_DI,
    IndicatorPatternConfig,
    align_30m_features_to_5m_bars,
    apply_indicator_gate,
    compute_adx_di_scores,
    compute_ema_band_scores,
    compose_breakout_score,
    config_for_variant,
    extract_breakout_events,
    extract_trend_follow_events,
    replay_indicator_variant,
)
from research.regime_scanner.trend_pine_export import (
    build_c3_regime_pine,
    build_pine_header,
    c3_state_code,
    export_c3_pine_bundle,
    validate_pine_script,
)
from research.regime_scanner.trend_regime_classification_audit import (
    ANALYZE_END,
    ANALYZE_START,
    C2_BASELINE_HASH,
    LOAD_END,
    LOAD_START,
    assert_baseline_readonly,
    enrich_timeline_outcomes,
    load_analysis_frame,
)
from research.regime_scanner.trend_regime_classifier import (
    config_c3,
    c3_direction,
    precompute_regime_arrays,
    replay_regime_variant,
)
from research.regime_scanner.trend_state_forward_outcome_audit import (
    build_price_arrays,
    compute_horizon_outcome,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c3_2b_d_ablation_smoke")
DEFAULT_BASELINE = Path("research/regime_scanner/results/baselines/c2_loose_mar_2026_before_c3")
DEFAULT_HORIZONS: tuple[int, ...] = (3, 6, 12, 24, 48)
PINE_VARIANT_FILES: dict[str, str] = {
    VARIANT_BASELINE: "trend_audit_c3_2b_baseline.pine",
    VARIANT_EMA: "trend_audit_c3_2c_ema.pine",
    VARIANT_EMA_ADX_DI: "trend_audit_c3_2d_ema_adx_di.pine",
}
COMPARISON_PINE_FILE = "trend_audit_c3_2_ablation_comparison.pine"
RECOMMENDED_PINE_FILE = "recommended.pine"


@dataclass(frozen=True)
class AuditVariantResult:
    variant: str
    mode: str
    config: dict[str, Any]
    replay: dict[str, Any]
    timeline: list[dict[str, Any]]
    breakout_events: list[dict[str, Any]]
    breakout_outcomes: list[dict[str, Any]]
    trend_follow_events: list[dict[str, Any]]
    trend_follow_outcomes: list[dict[str, Any]]
    regime_segments: list[dict[str, Any]]
    metrics: dict[str, Any]


def _ts(value: object) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _iso(value: object | None) -> str | None:
    if value is None:
        return None
    return _ts(value).isoformat()


def _finite(value: object, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _mean(values: Sequence[float]) -> float | None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return float(sum(vals) / len(vals)) if vals else None


def _median(values: Sequence[float]) -> float | None:
    vals = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    return float(statistics.median(vals)) if vals else None


def _share_true(values: Sequence[object]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(sum(1 for v in vals if bool(v)) / len(vals))


def _safe_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _direction_side(direction: str | None) -> str | None:
    if direction == "up":
        return "long"
    if direction == "down":
        return "short"
    return None


def _variant_pine_path(output_dir: Path, variant: str) -> Path:
    return output_dir / PINE_VARIANT_FILES[variant]


def _state_runs(timeline: Sequence[Mapping[str, Any]], *, variant: str) -> list[dict[str, Any]]:
    if not timeline:
        return []
    rows = sorted(timeline, key=lambda r: int(r["bar_index"]))
    runs: list[dict[str, Any]] = []
    cur_state = str(rows[0].get("state") or "unclear")
    start_i = int(rows[0]["bar_index"])
    start_ts = str(rows[0].get("decision_time") or "")
    prev_i = start_i
    prev_ts = start_ts
    for row in rows[1:]:
        bi = int(row["bar_index"])
        st = str(row.get("state") or "unclear")
        if st != cur_state:
            runs.append(
                {
                    "variant": variant,
                    "state": cur_state,
                    "state_code": c3_state_code(cur_state),
                    "start_bar_index": start_i,
                    "end_bar_index": prev_i,
                    "start_time": start_ts,
                    "end_time": prev_ts,
                    "duration_bars": prev_i - start_i + 1,
                }
            )
            cur_state = st
            start_i = bi
            start_ts = str(row.get("decision_time") or "")
        prev_i = bi
        prev_ts = str(row.get("decision_time") or "")
    runs.append(
        {
            "variant": variant,
            "state": cur_state,
            "state_code": c3_state_code(cur_state),
            "start_bar_index": start_i,
            "end_bar_index": prev_i,
            "start_time": start_ts,
            "end_time": prev_ts,
            "duration_bars": prev_i - start_i + 1,
        }
    )
    return runs


def _transition_rows(timeline: Sequence[Mapping[str, Any]], *, variant: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in timeline:
        if not row.get("transition"):
            continue
        rows.append(
            {
                "variant": variant,
                "decision_time": row.get("decision_time"),
                "bar_index": int(row["bar_index"]),
                "previous_state": row.get("previous_state"),
                "new_state": row.get("state"),
                "previous_state_code": c3_state_code(row.get("previous_state")),
                "new_state_code": c3_state_code(row.get("state")),
                "parent_trend": row.get("parent_trend"),
                "range_score": row.get("range_score"),
                "ema_state": row.get("ema_state"),
                "gate_reason": row.get("gate_reason"),
            }
        )
    return rows


def _normalize_event_row(row: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if "event_id" in out and "breakout_id" not in out:
        out["breakout_id"] = out["event_id"]
    if "start_time" in out and "attempt_time" not in out:
        out["attempt_time"] = out.get("start_time")
    if "start_bar_index" in out and "attempt_bar_index" not in out:
        out["attempt_bar_index"] = out.get("start_bar_index")
    if "start_close" in out and "attempt_close" not in out:
        out["attempt_close"] = out.get("start_close")
    if "start_state" in out and "attempt_state" not in out:
        out["attempt_state"] = out.get("start_state")
    if "start_c31_state" in out and "attempt_c31_state" not in out:
        out["attempt_c31_state"] = out.get("start_c31_state")
    if "lifecycle_outcome" in out and "result" not in out:
        out["result"] = out.get("lifecycle_outcome")
    return out


def _event_anchor_specs(event: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    return [
        ("attempt", "attempt_bar_index", "attempt_close"),
        ("confirm", "confirm_bar_index", "confirm_close"),
    ]


def _enrich_event_outcomes(
    events: Sequence[Mapping[str, Any]],
    *,
    arrays: dict[str, Any],
    horizons: tuple[int, ...],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for raw in events:
        row = _normalize_event_row(raw)
        direction = str(row.get("direction") or "")
        side = _direction_side(direction)
        for prefix, bar_key, close_key in _event_anchor_specs(row):
            bar_i = _safe_int(row.get(bar_key))
            ref_close = row.get(close_key)
            if bar_i is None or ref_close is None or pd.isna(ref_close):
                for h in horizons:
                    for suffix in (
                        "evaluable",
                        "reason",
                        "raw_close_return_pct",
                        "directional_close_return_pct",
                        "direction_hit",
                        "mfe_pct",
                        "mae_pct",
                        "mfe_peak_offset",
                        "mae_peak_offset",
                        "bars_to_first_positive_directional",
                        "mfe_mae_ratio",
                    ):
                        row[f"{prefix}_h{h}_{suffix}"] = None
                continue
            for h in horizons:
                outcome = compute_horizon_outcome(
                    bar_index=bar_i,
                    horizon=int(h),
                    reference_close=float(ref_close),
                    side=side,
                    arrays=arrays,
                )
                for key, value in outcome.items():
                    if key == "horizon":
                        continue
                    row[f"{prefix}_h{h}_{key}"] = value
        enriched.append(row)
    return enriched


def _component_rows(
    *,
    variant: str,
    kind: str,
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if not events:
        return [
            {
                "variant": variant,
                "kind": kind,
                "component": "none",
                "metric": "mean_score",
                "value": None,
                "n_events": 0,
            }
        ]

    if kind == "breakout":
        comps = {
            "ema_only": [float(e.get("attempt_ema_score") or 0.0) for e in events],
            "ema_plus_di": [
                (float(e.get("attempt_ema_score") or 0.0) + float(e.get("attempt_di_score") or 0.0))
                / 2.0
                for e in events
            ],
            "ema_plus_di_plus_adx": [
                (
                    float(e.get("attempt_ema_score") or 0.0)
                    + float(e.get("attempt_di_score") or 0.0)
                    + float(e.get("attempt_adx_score") or 0.0)
                )
                / 3.0
                for e in events
            ],
        }
    else:
        comps = {
            "support_only": [float(e.get("attempt_support_score") or 0.0) for e in events],
            "support_plus_reaccel": [
                (
                    float(e.get("attempt_support_score") or 0.0)
                    + float(e.get("attempt_reaccel_score") or 0.0)
                )
                / 2.0
                for e in events
            ],
            "support_plus_reaccel_plus_adx": [
                (
                    float(e.get("attempt_support_score") or 0.0)
                    + float(e.get("attempt_reaccel_score") or 0.0)
                    + float(e.get("attempt_adx_score") or 0.0)
                )
                / 3.0
                for e in events
            ],
        }

    rows: list[dict[str, Any]] = []
    for component, values in comps.items():
        rows.append(
            {
                "variant": variant,
                "kind": kind,
                "component": component,
                "metric": "mean_score",
                "value": _mean(values),
                "n_events": len(values),
            }
        )
        rows.append(
            {
                "variant": variant,
                "kind": kind,
                "component": component,
                "metric": "median_score",
                "value": _median(values),
                "n_events": len(values),
            }
        )
        rows.append(
            {
                "variant": variant,
                "kind": kind,
                "component": component,
                "metric": "share_ge_0.5",
                "value": _share_true([v >= 0.5 for v in values]),
                "n_events": len(values),
            }
        )
    return rows


def _ping_pong_metrics(timeline: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(r) for r in timeline if r.get("transition")]
    if not rows:
        return {f"ping_pong_within_{w}": None for w in (1, 3, 6, 12)}
    durations: list[int] = []
    for i, row in enumerate(rows):
        cur = str(row.get("state") or "")
        next_index = None
        for nxt in rows[i + 1 :]:
            if str(nxt.get("state") or "") != cur:
                next_index = int(nxt["bar_index"])
                break
        if next_index is None:
            durations.append(10**9)
        else:
            durations.append(max(1, next_index - int(row["bar_index"])))
    out: dict[str, Any] = {}
    for w in (1, 3, 6, 12):
        out[f"ping_pong_within_{w}"] = float(sum(1 for d in durations if d <= w) / len(durations))
    out["transition_rate"] = float(len(rows) / max(1, len(timeline)))
    return out


def _timeline_state_shares(timeline: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = max(1, len(timeline))
    counts: dict[str, int] = {}
    for row in timeline:
        st = str(row.get("state") or "unclear")
        counts[st] = counts.get(st, 0) + 1
    return {f"share_{state}": count / total for state, count in sorted(counts.items())}


def _horizon_summary(
    events: Sequence[Mapping[str, Any]],
    *,
    prefix: str,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for h in horizons:
        evaluable = [e for e in events if e.get(f"{prefix}_h{h}_evaluable") is True]
        hits = [
            e.get(f"{prefix}_h{h}_direction_hit")
            for e in evaluable
            if e.get(f"{prefix}_h{h}_direction_hit") is not None
        ]
        raw = [
            _finite(e.get(f"{prefix}_h{h}_raw_close_return_pct"))
            for e in evaluable
            if e.get(f"{prefix}_h{h}_raw_close_return_pct") is not None
        ]
        dret = [
            _finite(e.get(f"{prefix}_h{h}_directional_close_return_pct"))
            for e in evaluable
            if e.get(f"{prefix}_h{h}_directional_close_return_pct") is not None
        ]
        mfe = [
            _finite(e.get(f"{prefix}_h{h}_mfe_pct"))
            for e in evaluable
            if e.get(f"{prefix}_h{h}_mfe_pct") is not None
        ]
        mae = [
            _finite(e.get(f"{prefix}_h{h}_mae_pct"))
            for e in evaluable
            if e.get(f"{prefix}_h{h}_mae_pct") is not None
        ]
        summary[f"h{h}"] = {
            "n_evaluable": len(evaluable),
            "hit_rate": _share_true(hits),
            "mean_raw_close_return_pct": _mean(raw),
            "mean_directional_close_return_pct": _mean(dret),
            "mean_mfe_pct": _mean(mfe),
            "mean_mae_pct": _mean(mae),
            "mean_mfe_mae_ratio": _mean(
                [
                    _finite(e.get(f"{prefix}_h{h}_mfe_mae_ratio"))
                    for e in evaluable
                    if e.get(f"{prefix}_h{h}_mfe_mae_ratio") is not None
                ]
            ),
        }
    return summary


def _breakout_metrics(
    events: Sequence[Mapping[str, Any]],
    *,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    confirmed = [e for e in events if str(e.get("lifecycle_outcome") or "") == "confirmed"]
    failed = [e for e in events if str(e.get("lifecycle_outcome") or "") == "failed"]
    timeout = [e for e in events if str(e.get("lifecycle_outcome") or "") == "timeout"]
    reentered = [e for e in events if str(e.get("lifecycle_outcome") or "") == "reentered"]
    delays = [
        _safe_int(e.get("confirm_bar_index")) - _safe_int(e.get("attempt_bar_index"))
        for e in confirmed
        if _safe_int(e.get("confirm_bar_index")) is not None and _safe_int(e.get("attempt_bar_index")) is not None
    ]
    delays = [int(d) for d in delays if d is not None]
    parent_retained = [
        1.0
        if _direction_side(c3_direction(str(e.get("confirm_state") or e.get("end_state") or "")))
        == _direction_side(str(e.get("parent_trend") or ""))
        and _direction_side(str(e.get("parent_trend") or "")) is not None
        else 0.0
        for e in confirmed
    ]
    return {
        "attempts": len(events),
        "confirmed": len(confirmed),
        "failed": len(failed),
        "timeout": len(timeout),
        "reentered": len(reentered),
        "success_rate": float(len(confirmed) / len(events)) if events else None,
        "fail_rate": float(len(failed) / len(events)) if events else None,
        "timeout_rate": float(len(timeout) / len(events)) if events else None,
        "reentered_rate": float(len(reentered) / len(events)) if events else None,
        "mean_confirmation_delay_bars": _mean([float(d) for d in delays]),
        "median_confirmation_delay_bars": _median([float(d) for d in delays]),
        "parent_retention_rate": _share_true(parent_retained),
        "attempt_horizons": _horizon_summary(events, prefix="attempt", horizons=horizons),
        "confirm_horizons": _horizon_summary(confirmed, prefix="confirm", horizons=horizons),
    }


def _trend_follow_metrics(
    events: Sequence[Mapping[str, Any]],
    *,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    confirmed = [e for e in events if str(e.get("lifecycle_outcome") or "") == "confirmed"]
    failed = [e for e in events if str(e.get("lifecycle_outcome") or "") == "failed"]
    timeout = [e for e in events if str(e.get("lifecycle_outcome") or "") == "timeout"]
    parent_aligned = [
        1.0
        if _direction_side(str(e.get("parent_trend") or "")) == _direction_side(str(e.get("direction") or ""))
        and _direction_side(str(e.get("direction") or "")) is not None
        else 0.0
        for e in events
    ]
    return {
        "attempts": len(events),
        "confirmed": len(confirmed),
        "failed": len(failed),
        "timeout": len(timeout),
        "success_rate": float(len(confirmed) / len(events)) if events else None,
        "fail_rate": float(len(failed) / len(events)) if events else None,
        "timeout_rate": float(len(timeout) / len(events)) if events else None,
        "parent_alignment_rate": _share_true(parent_aligned),
        "attempt_horizons": _horizon_summary(events, prefix="attempt", horizons=horizons),
        "confirm_horizons": _horizon_summary(confirmed, prefix="confirm", horizons=horizons),
    }


def _variant_metrics(
    *,
    variant: str,
    timeline: Sequence[Mapping[str, Any]],
    breakout_events: Sequence[Mapping[str, Any]],
    breakout_outcomes: Sequence[Mapping[str, Any]],
    trend_follow_events: Sequence[Mapping[str, Any]],
    trend_follow_outcomes: Sequence[Mapping[str, Any]],
    horizons: tuple[int, ...],
    c31_parity_ok: bool | None = None,
) -> dict[str, Any]:
    n_bars = len(timeline)
    state_shares = _timeline_state_shares(timeline)
    ping = _ping_pong_metrics(timeline)
    breakout = _breakout_metrics(breakout_outcomes, horizons=horizons)
    trend = _trend_follow_metrics(trend_follow_outcomes, horizons=horizons)
    regime_segments = _state_runs(timeline, variant=variant)
    component_rows = _component_rows(variant=variant, kind="breakout", events=breakout_outcomes) + _component_rows(
        variant=variant, kind="trend_follow", events=trend_follow_outcomes
    )
    return {
        "variant": variant,
        "n_timeline_bars": n_bars,
        "n_transitions": sum(1 for row in timeline if row.get("transition")),
        "c31_parity_ok": c31_parity_ok,
        "regime_shares": state_shares,
        "ping_pong": ping,
        "breakout": breakout,
        "trend_follow": trend,
        "regime_segments": regime_segments,
        "indicator_condition_stats": component_rows,
    }


def _compare_pair(
    left_variant: str,
    right_variant: str,
    left_metrics: Mapping[str, Any],
    right_metrics: Mapping[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    metrics = [
        ("breakout.success_rate", "higher"),
        ("breakout.fail_rate", "lower"),
        ("breakout.mean_confirmation_delay_bars", "lower"),
        ("breakout.parent_retention_rate", "higher"),
        ("trend_follow.success_rate", "higher"),
        ("trend_follow.parent_alignment_rate", "higher"),
        ("ping_pong.ping_pong_within_3", "lower"),
        ("ping_pong.transition_rate", "lower"),
    ]
    def _lookup_metric(container: Mapping[str, Any], path: str) -> Any:
        cur: Any = container
        for part in path.split("."):
            if not isinstance(cur, Mapping):
                return None
            cur = cur.get(part)
        return cur

    for metric, preferred in metrics:
        left_value = _lookup_metric(left_metrics, metric)
        right_value = _lookup_metric(right_metrics, metric)
        row = {
            "from_variant": left_variant,
            "to_variant": right_variant,
            "metric": metric,
            "preferred_direction": preferred,
            "from_value": left_value if not isinstance(left_value, Mapping) else None,
            "to_value": right_value if not isinstance(right_value, Mapping) else None,
            "delta": None,
        }
        if isinstance(row["from_value"], (int, float)) and isinstance(row["to_value"], (int, float)):
            row["delta"] = float(row["to_value"]) - float(row["from_value"])
        rows.append(row)
    return rows


def _collect_scalar_metrics(summary: Mapping[str, Any]) -> dict[str, Any]:
    breakout = summary["breakout"]
    trend = summary["trend_follow"]
    ping = summary["ping_pong"]
    out = {
        "variant": summary["variant"],
        "n_timeline_bars": summary["n_timeline_bars"],
        "n_transitions": summary["n_transitions"],
        "c31_parity_ok": summary.get("c31_parity_ok"),
        "breakout_attempts": breakout["attempts"],
        "breakout_confirmed": breakout["confirmed"],
        "breakout_failed": breakout["failed"],
        "breakout_timeout": breakout["timeout"],
        "breakout_reentered": breakout["reentered"],
        "breakout_success_rate": breakout["success_rate"],
        "breakout_fail_rate": breakout["fail_rate"],
        "breakout_timeout_rate": breakout["timeout_rate"],
        "breakout_reentered_rate": breakout["reentered_rate"],
        "breakout_mean_confirmation_delay_bars": breakout["mean_confirmation_delay_bars"],
        "breakout_parent_retention_rate": breakout["parent_retention_rate"],
        "trend_follow_attempts": trend["attempts"],
        "trend_follow_confirmed": trend["confirmed"],
        "trend_follow_failed": trend["failed"],
        "trend_follow_timeout": trend["timeout"],
        "trend_follow_success_rate": trend["success_rate"],
        "trend_follow_fail_rate": trend["fail_rate"],
        "trend_follow_parent_alignment_rate": trend["parent_alignment_rate"],
        "ping_pong_within_1_rate": ping["ping_pong_within_1"],
        "ping_pong_within_3_rate": ping["ping_pong_within_3"],
        "ping_pong_within_6_rate": ping["ping_pong_within_6"],
        "ping_pong_within_12_rate": ping["ping_pong_within_12"],
        "transition_rate": ping["transition_rate"],
    }
    for key, value in summary["regime_shares"].items():
        out[key] = value
    for h, metrics in breakout["attempt_horizons"].items():
        for metric_name, metric_value in metrics.items():
            out[f"breakout_attempt_{h}_{metric_name}"] = metric_value
    for h, metrics in breakout["confirm_horizons"].items():
        for metric_name, metric_value in metrics.items():
            out[f"breakout_confirm_{h}_{metric_name}"] = metric_value
    for h, metrics in trend["attempt_horizons"].items():
        for metric_name, metric_value in metrics.items():
            out[f"trend_follow_attempt_{h}_{metric_name}"] = metric_value
    for h, metrics in trend["confirm_horizons"].items():
        for metric_name, metric_value in metrics.items():
            out[f"trend_follow_confirm_{h}_{metric_name}"] = metric_value
    return out


def _build_manual_review_anchors() -> list[dict[str, Any]]:
    return [
        {
            "anchor_id": "mar3_mar6_range_build",
            "start_time": "2026-03-03T00:00:00+00:00",
            "end_time": "2026-03-06T23:55:00+00:00",
            "note": "Soft review window for the Mar 3-6 range build-up and transition quality.",
        },
        {
            "anchor_id": "mar6_breakdown_start",
            "start_time": "2026-03-06T00:00:00+00:00",
            "end_time": "2026-03-06T23:55:00+00:00",
            "note": "Downside trigger window to inspect the initial post-range break.",
        },
        {
            "anchor_id": "post_mar6_downside_followthrough",
            "start_time": "2026-03-07T00:00:00+00:00",
            "end_time": "2026-03-10T23:55:00+00:00",
            "note": "Review follow-through after the first downside break.",
        },
        {
            "anchor_id": "late_march_downside",
            "start_time": "2026-03-11T00:00:00+00:00",
            "end_time": "2026-03-12T23:55:00+00:00",
            "note": "Optional downstream confirmation / failure check window.",
        },
    ]


def _compare_timeline_parity(
    baseline_timeline: Sequence[Mapping[str, Any]],
    c31_timeline: Sequence[Mapping[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    left = {int(row["bar_index"]): str(row.get("state") or "") for row in baseline_timeline}
    right = {int(row["bar_index"]): str(row.get("state") or "") for row in c31_timeline}
    common = sorted(set(left) & set(right))
    mismatches: list[dict[str, Any]] = []
    for bi in common:
        if left[bi] != right[bi]:
            mismatches.append({"bar_index": bi, "baseline_state": left[bi], "c31_state": right[bi]})
            if len(mismatches) >= 20:
                break
    ok = not mismatches and len(left) == len(right)
    return ok, mismatches


def _build_comparison_pine(
    *,
    title: str,
    symbol: str,
    analyze_start: str,
    analyze_end: str,
    baseline_variant: str,
    recommended_variant: str,
    baseline_metrics: Mapping[str, Any],
    recommended_metrics: Mapping[str, Any],
) -> str:
    lines = [
        *build_pine_header(title),
        f"// Simplified comparison: {baseline_variant} vs {recommended_variant}",
        f"// Symbol: {symbol} | Analyze: {analyze_start} .. {analyze_end}",
        "// Read-only review artifact. No strategy logic.",
        "",
        'showTable = input.bool(true, "Show summary table")',
        "",
        "if showTable and barstate.islast",
        "    var table t = table.new(position.top_right, 3, 5, bgcolor=color.new(color.black, 35))",
        '    table.cell(t, 0, 0, "Metric", text_color=color.white)',
        '    table.cell(t, 1, 0, "Baseline", text_color=color.white)',
        '    table.cell(t, 2, 0, "Recommended", text_color=color.white)',
        '    table.cell(t, 0, 1, "Breakout success", text_color=color.white)',
        f'    table.cell(t, 1, 1, str.tostring({float(baseline_metrics["breakout"]["success_rate"] or 0.0):.4f}), text_color=color.white)',
        f'    table.cell(t, 2, 1, str.tostring({float(recommended_metrics["breakout"]["success_rate"] or 0.0):.4f}), text_color=color.white)',
        '    table.cell(t, 0, 2, "Trend-follow success", text_color=color.white)',
        f'    table.cell(t, 1, 2, str.tostring({float(baseline_metrics["trend_follow"]["success_rate"] or 0.0):.4f}), text_color=color.white)',
        f'    table.cell(t, 2, 2, str.tostring({float(recommended_metrics["trend_follow"]["success_rate"] or 0.0):.4f}), text_color=color.white)',
        '    table.cell(t, 0, 3, "Ping-pong <= 3", text_color=color.white)',
        f'    table.cell(t, 1, 3, str.tostring({float(baseline_metrics["ping_pong"]["ping_pong_within_3"] or 0.0):.4f}), text_color=color.white)',
        f'    table.cell(t, 2, 3, str.tostring({float(recommended_metrics["ping_pong"]["ping_pong_within_3"] or 0.0):.4f}), text_color=color.white)',
        '    table.cell(t, 0, 4, "C3.1 parity", text_color=color.white)',
        f'    table.cell(t, 1, 4, "{str(bool(baseline_metrics.get("c31_parity_ok"))).lower()}", text_color=color.white)',
        '    table.cell(t, 2, 4, "n/a", text_color=color.white)',
        "",
        "// EOF",
    ]
    pine_text = "\n".join(lines) + "\n"
    validate_pine_script(pine_text)
    return pine_text


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(list(rows)).to_csv(path, index=False)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.write_text(json.dumps(json_safe(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _deterministic_hash(payload: Mapping[str, Any]) -> str:
    blob = json.dumps(json_safe(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _export_pine_artifacts(
    *,
    output_dir: Path,
    symbol: str,
    analyze_start: str,
    analyze_end: str,
    variant_results: Mapping[str, AuditVariantResult],
    recommended_variant: str,
    summary_by_variant: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    variant_payloads = {
        variant: {
            "timeline_rows": list(result.timeline),
            "marker_rows": [
                {
                    "decision_time": row.get("decision_time"),
                    "new_state": row.get("state"),
                    "label": f'{row.get("previous_state")}->{row.get("state")}',
                }
                for row in result.timeline
                if row.get("transition")
            ],
        }
        for variant, result in variant_results.items()
    }

    pine_meta: dict[str, Any] = {}
    per_variant: dict[str, Any] = {}
    for variant, result in variant_results.items():
        title = f"{symbol} C3.2 {variant}"
        pine_text = build_c3_regime_pine(
            title=title,
            symbol=symbol,
            phase="C3_2_indicator_pattern_audit",
            variant=variant,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            audit_hash=summary_by_variant[variant]["summary_hash"],
            state_runs=result.regime_segments,
            transitions=_transition_rows(result.timeline, variant=variant),
            markers=[],
            timeline_rows=result.timeline,
        )
        pine_path = _variant_pine_path(output_dir, variant)
        pine_path.write_text(pine_text, encoding="utf-8")
        validate_pine_script(pine_text)
        per_variant[variant] = {
            "pine_path": str(pine_path),
            "pine_sha256": hashlib.sha256(pine_text.encode()).hexdigest(),
            "title": title,
        }

    recommended_src = _variant_pine_path(output_dir, recommended_variant)
    recommended_dst = output_dir / RECOMMENDED_PINE_FILE
    shutil.copyfile(recommended_src, recommended_dst)
    baseline_variant = VARIANT_BASELINE
    comparison_text = _build_comparison_pine(
        title=f"{symbol} C3.2 ablation comparison",
        symbol=symbol,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        baseline_variant=baseline_variant,
        recommended_variant=recommended_variant,
        baseline_metrics=variant_results[baseline_variant].metrics,
        recommended_metrics=variant_results[recommended_variant].metrics,
    )
    comparison_path = output_dir / COMPARISON_PINE_FILE
    comparison_path.write_text(comparison_text, encoding="utf-8")
    validate_pine_script(comparison_text)
    pine_meta["variants"] = per_variant
    pine_meta["recommended_pine"] = str(recommended_dst)
    pine_meta["comparison_pine"] = str(comparison_path)
    pine_meta["variant_payloads"] = variant_payloads
    return pine_meta


def _sensitivity_checks(
    *,
    symbol: str,
    prepared_bars: Sequence[Any],
    arrays: dict[str, Any],
    c31_cfg: Any,
    indicator_rows: Mapping[int, Mapping[str, Any] | None],
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    base_variant: str = VARIANT_EMA,
) -> list[dict[str, Any]]:
    cfg = config_for_variant(base_variant)
    target_fields = [
        ("breakout_ema_confirmation_min", cfg.breakout_ema_confirmation_min),
        ("ema_fast_expansion_min", cfg.ema_fast_expansion_min),
    ]
    rows: list[dict[str, Any]] = []
    for field, base_value in target_fields:
        for scale in (0.9, 1.1):
            candidate = config_for_variant(base_variant, **{field: float(base_value) * scale})
            replay = replay_indicator_variant(
                prepared_bars,
                arrays,
                c31_cfg,
                candidate,
                indicator_rows,
                analyze_start,
                analyze_end,
            )
            timeline = replay["timeline"]
            breakout = extract_breakout_events(timeline, candidate.variant_id, symbol, "5m", candidate)
            trend = extract_trend_follow_events(timeline, candidate.variant_id, symbol, "5m", candidate)
            rows.append(
                {
                    "variant": candidate.variant_id,
                    "field": field,
                    "scale": scale,
                    "value": float(getattr(candidate, field)),
                    "breakout_attempts": len(breakout),
                    "trend_follow_attempts": len(trend),
                    "breakout_success_rate": _breakout_metrics(breakout, horizons=DEFAULT_HORIZONS)[
                        "success_rate"
                    ],
                    "trend_follow_success_rate": _trend_follow_metrics(
                        trend, horizons=DEFAULT_HORIZONS
                    )["success_rate"],
                }
            )
    return rows


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    baseline_dir: Path = DEFAULT_BASELINE,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    export_pine: bool = True,
    run_sensitivity: bool = True,
    c31_variant: str = "conservative",
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    baseline_info = assert_baseline_readonly(baseline_dir)
    if not baseline_info.get("hash_matches"):
        raise RuntimeError(
            f"baseline hash mismatch: expected {C2_BASELINE_HASH}, got {baseline_info.get('baseline_hash')}"
        )

    t0 = time.perf_counter()
    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)

    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    shared = load_or_build_shared_context(frame, cache_dir=output_dir / ".cache")
    features_30m = load_or_build_indicator_features(
        symbol=symbol,
        timeframe="30m",
        analyze_start=load_start,
        analyze_end=load_end,
        cache_dir=output_dir / ".cache" / "indicator_features",
    )
    attach_c32a_indicator_features(shared, features_30m, feature_version=INDICATOR_FEATURE_VERSION)
    aligned_30m = align_30m_features_to_5m_bars(shared.prepared_bars, features_30m)
    indicator_by_index = {
        prep.bar_index: aligned_30m[i] for i, prep in enumerate(shared.prepared_bars)
    }
    price_arrays = build_price_arrays(frame)
    c31_cfg = config_c3(c31_variant)
    regime_arrays = precompute_regime_arrays(
        frame,
        efficiency_window=c31_cfg.efficiency_window,
        net_move_window=c31_cfg.net_move_window,
        overlap_window=c31_cfg.overlap_window,
        range_width_window=c31_cfg.range_width_window,
        range_lookback=c31_cfg.range_lookback,
        failed_breakout_window=c31_cfg.failed_breakout_window,
        alternating_window=c31_cfg.alternating_window,
    )

    c31_replay = replay_regime_variant(
        shared.prepared_bars,
        arrays=regime_arrays,
        cfg=c31_cfg,
        analyze_start=a0,
        analyze_end=a1,
    )

    variant_results: dict[str, AuditVariantResult] = {}
    summary_by_variant: dict[str, dict[str, Any]] = {}
    all_states: list[dict[str, Any]] = []
    breakout_events_all: list[dict[str, Any]] = []
    breakout_outcomes_all: list[dict[str, Any]] = []
    trend_follow_events_all: list[dict[str, Any]] = []
    trend_follow_outcomes_all: list[dict[str, Any]] = []
    regime_segments_all: list[dict[str, Any]] = []
    indicator_condition_stats_all: list[dict[str, Any]] = []
    ablation_metrics_rows: list[dict[str, Any]] = []
    variant_comparison_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    timeline_rows_all: list[dict[str, Any]] = []
    c31_parity_ok = None
    c31_parity_mismatches: list[dict[str, Any]] = []

    for variant in ALL_VARIANTS:
        cfg = config_for_variant(variant)
        replay = replay_indicator_variant(
            shared.prepared_bars,
            regime_arrays,
            c31_cfg,
            cfg,
            indicator_by_index,
            a0,
            a1,
        )
        timeline = enrich_timeline_outcomes(
            list(replay["timeline"]),
            arrays=price_arrays,
            horizons=horizons,
        )
        breakout_events = extract_breakout_events(timeline, variant, symbol, "5m", cfg)
        trend_follow_events = extract_trend_follow_events(timeline, variant, symbol, "5m", cfg)
        breakout_outcomes = _enrich_event_outcomes(
            breakout_events, arrays=price_arrays, horizons=horizons
        )
        trend_follow_outcomes = _enrich_event_outcomes(
            trend_follow_events, arrays=price_arrays, horizons=horizons
        )
        metrics = _variant_metrics(
            variant=variant,
            timeline=timeline,
            breakout_events=breakout_events,
            breakout_outcomes=breakout_outcomes,
            trend_follow_events=trend_follow_events,
            trend_follow_outcomes=trend_follow_outcomes,
            horizons=horizons,
            c31_parity_ok=None,
        )

        if variant == VARIANT_BASELINE:
            c31_parity_ok, c31_parity_mismatches = _compare_timeline_parity(
                timeline, c31_replay["timeline"]
            )
            metrics["c31_parity_ok"] = c31_parity_ok
            metrics["c31_parity_mismatches"] = c31_parity_mismatches

        summary = _collect_scalar_metrics(metrics)
        summary_hash = _deterministic_hash(
            {
                "variant": variant,
                "metrics": summary,
                "parity": metrics.get("c31_parity_ok"),
            }
        )
        summary["summary_hash"] = summary_hash
        summary_by_variant[variant] = summary
        ablation_metrics_rows.extend(
            [
                {"variant": variant, "metric": k, "value": v}
                for k, v in summary.items()
                if isinstance(v, (int, float, bool)) or v is None
            ]
        )
        indicator_condition_stats_all.extend(metrics["indicator_condition_stats"])
        variant_results[variant] = AuditVariantResult(
            variant=variant,
            mode=cfg.mode,
            config=cfg.to_dict(),
            replay=replay,
            timeline=timeline,
            breakout_events=breakout_events,
            breakout_outcomes=breakout_outcomes,
            trend_follow_events=trend_follow_events,
            trend_follow_outcomes=trend_follow_outcomes,
            regime_segments=_state_runs(timeline, variant=variant),
            metrics=metrics,
        )

        timeline_rows_all.extend([{**row, "variant": variant} for row in timeline])
        all_states.extend([{**row, "variant": variant} for row in timeline])
        breakout_events_all.extend(breakout_events)
        breakout_outcomes_all.extend(breakout_outcomes)
        trend_follow_events_all.extend(trend_follow_events)
        trend_follow_outcomes_all.extend(trend_follow_outcomes)
        regime_segments_all.extend(metrics["regime_segments"])

    # Pairwise deltas across the ablation ladder.
    comparison_rows.extend(
        _compare_pair(
            VARIANT_BASELINE,
            VARIANT_EMA,
            variant_results[VARIANT_BASELINE].metrics,
            variant_results[VARIANT_EMA].metrics,
        )
    )
    comparison_rows.extend(
        _compare_pair(
            VARIANT_EMA,
            VARIANT_EMA_ADX_DI,
            variant_results[VARIANT_EMA].metrics,
            variant_results[VARIANT_EMA_ADX_DI].metrics,
        )
    )

    recommended_variant = max(
        summary_by_variant,
        key=lambda variant: (
            float(summary_by_variant[variant].get("breakout_success_rate") or 0.0) * 2.0
            + float(summary_by_variant[variant].get("trend_follow_success_rate") or 0.0) * 1.5
            + float(summary_by_variant[variant].get("breakout_parent_retention_rate") or 0.0) * 0.75
            + float(summary_by_variant[variant].get("trend_follow_parent_alignment_rate") or 0.0) * 0.75
            - float(summary_by_variant[variant].get("ping_pong_within_3_rate") or 0.0) * 0.75
            - float(summary_by_variant[variant].get("transition_rate") or 0.0) * 0.25
        ),
    )
    recommendation = {
        "recommended_variant": recommended_variant,
        "recommended_mode": variant_results[recommended_variant].mode,
        "reason": (
            "Maximizes breakout and trend-follow success while penalizing ping-pong and churn."
        ),
        "scoring": {
            variant: {
                "score": (
                    float(summary_by_variant[variant].get("breakout_success_rate") or 0.0) * 2.0
                    + float(summary_by_variant[variant].get("trend_follow_success_rate") or 0.0) * 1.5
                    + float(summary_by_variant[variant].get("breakout_parent_retention_rate") or 0.0)
                    * 0.75
                    + float(summary_by_variant[variant].get("trend_follow_parent_alignment_rate") or 0.0)
                    * 0.75
                    - float(summary_by_variant[variant].get("ping_pong_within_3_rate") or 0.0) * 0.75
                    - float(summary_by_variant[variant].get("transition_rate") or 0.0) * 0.25
                ),
                "breakout_success_rate": summary_by_variant[variant].get("breakout_success_rate"),
                "trend_follow_success_rate": summary_by_variant[variant].get("trend_follow_success_rate"),
                "ping_pong_within_3_rate": summary_by_variant[variant].get("ping_pong_within_3_rate"),
            }
            for variant in summary_by_variant
        },
    }

    sensitivity_rows: list[dict[str, Any]] = []
    if run_sensitivity:
        sensitivity_rows = _sensitivity_checks(
            symbol=symbol,
            prepared_bars=shared.prepared_bars,
            arrays=regime_arrays,
            c31_cfg=c31_cfg,
            indicator_rows=indicator_by_index,
            analyze_start=a0,
            analyze_end=a1,
        )

    if export_pine:
        pine_meta = _export_pine_artifacts(
            output_dir=output_dir,
            symbol=symbol,
            analyze_start=analyze_start,
            analyze_end=analyze_end,
            variant_results=variant_results,
            recommended_variant=recommended_variant,
            summary_by_variant=summary_by_variant,
        )
    else:
        pine_meta = {"exported": False}

    states_csv_rows = timeline_rows_all
    regime_segment_rows = regime_segments_all
    comparison_csv_rows = comparison_rows
    manual_review_rows = _build_manual_review_anchors()

    _write_csv(output_dir / "states.csv", states_csv_rows)
    _write_csv(output_dir / "breakout_events.csv", breakout_events_all)
    _write_csv(output_dir / "breakout_outcomes.csv", breakout_outcomes_all)
    _write_csv(output_dir / "trend_follow_events.csv", trend_follow_events_all)
    _write_csv(output_dir / "trend_follow_outcomes.csv", trend_follow_outcomes_all)
    _write_csv(output_dir / "regime_segments.csv", regime_segment_rows)
    _write_csv(output_dir / "indicator_condition_stats.csv", indicator_condition_stats_all)
    _write_csv(output_dir / "ablation_metrics.csv", ablation_metrics_rows)
    _write_csv(output_dir / "comparison.csv", comparison_csv_rows)
    _write_csv(
        output_dir / "variant_comparison.csv",
        [
            {"variant": variant, **summary_by_variant[variant]}
            for variant in ALL_VARIANTS
        ],
    )
    _write_csv(output_dir / "manual_review_anchors.csv", manual_review_rows)
    if sensitivity_rows:
        _write_csv(output_dir / "sensitivity.csv", sensitivity_rows)

    performance = {
        "elapsed_seconds": round(time.perf_counter() - t0, 3),
        "shared_structure_passes": shared.structure_pass_count,
        "shared_cache_key": shared.cache_key,
        "indicator_feature_version": INDICATOR_FEATURE_VERSION,
        "baseline_hash_matches": bool(baseline_info.get("hash_matches")),
    }

    summary_core = {
        "phase": "C3_2B_D_indicator_pattern_audit",
        "symbol": symbol,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "horizons": list(horizons),
        "baseline": baseline_info,
        "baseline_reference_hash": C2_BASELINE_HASH,
        "c31_variant": c31_variant,
        "c31_parity_ok": c31_parity_ok,
        "c31_parity_mismatches": c31_parity_mismatches,
        "recommended": recommendation,
        "variant_metrics": summary_by_variant,
        "comparison": comparison_csv_rows,
        "indicator_condition_stats": indicator_condition_stats_all,
        "ablation_metrics": ablation_metrics_rows,
        "sensitivity": sensitivity_rows,
        "manual_review_anchors": manual_review_rows,
        "artifact_files": {
            "summary": "summary.json",
            "metadata": "metadata.json",
            "comparison": "comparison.csv",
            "breakout_events": "breakout_events.csv",
            "breakout_outcomes": "breakout_outcomes.csv",
            "trend_follow_events": "trend_follow_events.csv",
            "trend_follow_outcomes": "trend_follow_outcomes.csv",
            "regime_segments": "regime_segments.csv",
            "indicator_condition_stats": "indicator_condition_stats.csv",
            "ablation_metrics": "ablation_metrics.csv",
            "manual_review_anchors": "manual_review_anchors.csv",
            "states": "states.csv",
            "variant_comparison": "variant_comparison.csv",
            "sensitivity": "sensitivity.csv" if sensitivity_rows else None,
            "recommended_pine": RECOMMENDED_PINE_FILE if export_pine else None,
            "comparison_pine": COMPARISON_PINE_FILE if export_pine else None,
        },
        "notes": {
            "classification_features_supply_only": True,
            "classification_features_supply_only_note": (
                "30m indicator features are attached read-only to the shared context; "
                "they do not change the C3.1 regime classifier."
            ),
            "production_unchanged": True,
            "no_live_bot_changes": True,
        },
    }
    summary = {**summary_core, "performance": performance}
    summary["deterministic_hash"] = _deterministic_hash(summary_core)

    metadata = {
        **summary_core,
        "summary_hash": summary["deterministic_hash"],
        "performance": performance,
        "baseline_hash_expected": C2_BASELINE_HASH,
        "variant_configs": {variant: variant_results[variant].config for variant in ALL_VARIANTS},
        "pine": pine_meta,
        "feature_version": INDICATOR_FEATURE_VERSION,
    }

    _write_json(output_dir / "summary.json", summary)
    _write_json(output_dir / "metadata.json", metadata)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase C3.2B-D indicator-pattern audit")
    parser.add_argument("--symbol", default="APTUSDT")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline-dir", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--load-start", default=LOAD_START)
    parser.add_argument("--load-end", default=LOAD_END)
    parser.add_argument("--analyze-start", default=ANALYZE_START)
    parser.add_argument("--analyze-end", default=ANALYZE_END)
    parser.add_argument("--horizons", nargs="+", type=int, default=list(DEFAULT_HORIZONS))
    parser.add_argument("--c31-variant", default="conservative")
    parser.add_argument("--export-pine", action=argparse.BooleanOptionalAction, default=True)
    sensitivity = parser.add_mutually_exclusive_group()
    sensitivity.add_argument("--run-sensitivity", dest="run_sensitivity", action="store_true")
    sensitivity.add_argument("--no-sensitivity", dest="run_sensitivity", action="store_false")
    parser.set_defaults(run_sensitivity=True)
    args = parser.parse_args(argv)
    summary = run_audit(
        symbol=args.symbol,
        output_dir=args.output_dir,
        baseline_dir=args.baseline_dir,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        horizons=tuple(int(h) for h in args.horizons),
        export_pine=bool(args.export_pine),
        run_sensitivity=bool(args.run_sensitivity),
        c31_variant=str(args.c31_variant),
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "recommended_variant": summary["recommended"]["recommended_variant"],
                "c31_parity_ok": summary["c31_parity_ok"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
