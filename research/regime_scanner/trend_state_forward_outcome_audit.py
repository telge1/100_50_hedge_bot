"""Phase C2: forward-outcome audit for trend state transitions (research-only).

Compares C1-B loose vs C1-C strict on whether transitions into topping,
bottoming, and optional weakening states align with subsequent price action.

Reuses the shared structure timeline (no per-variant market-structure replay).

CLI:
  PYTHONPATH=. python3 -m research.regime_scanner.trend_state_forward_outcome_audit \\
    --symbol APTUSDT \\
    --load-start YYYY-MM-DD \\
    --load-end YYYY-MM-DD \\
    --analyze-start YYYY-MM-DD \\
    --analyze-end YYYY-MM-DD \\
    --variants loose strict \\
    --horizons 3 6 12 24 48 \\
    --output-dir PATH
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.config import default_regime_scanner_config
from research.regime_scanner.momentum_forward_audit import directional_close_return_pct
from research.regime_scanner.point_audit import json_safe
from research.regime_scanner.swings import find_confirmed_pivots
from research.regime_scanner.trend_audit_shared_replay import (
    SharedReplayContext,
    load_or_build_shared_context,
    reset_audit_counters,
    step_trend_state_from_prepared,
)
import research.regime_scanner.trend_audit_shared_replay as shared_replay_mod
from research.regime_scanner.trend_robustness_audit import (
    ANALYZE_END,
    ANALYZE_START,
    LOAD_END,
    LOAD_START,
    install_htf_cache,
    load_analysis_frame,
)
from research.regime_scanner.trend_state_machine import (
    TrendRuntime,
    WeakeningMultiBarMode,
    default_trend_state_config,
    step_trend_state,
    trend_state_config_c1,
)
from research.regime_scanner.trend_weakening_multi_bar_audit import assert_safe_output_dir
from research.regime_scanner.trend_pine_export import (
    build_timeline_from_state_series,
    export_audit_pine_artifacts,
    marker_rows_from_events,
)

DEFAULT_OUT = Path("research/regime_scanner/results/phase_c2_forward_outcome")
DEFAULT_HORIZONS: tuple[int, ...] = (3, 6, 12, 24, 48)
FAST_REVERSAL_WINDOWS: tuple[int, ...] = (3, 6, 12, 24)

VARIANT_ALIASES: dict[str, tuple[str, WeakeningMultiBarMode]] = {
    "loose": ("C1_B_loose", "loose"),
    "strict": ("C1_C_strict", "strict"),
}

CORE_ENTER_STATES: frozenset[str] = frozenset({"topping", "bottoming"})
OPTIONAL_WEAKENING_STATES: frozenset[str] = frozenset(
    {"bullish_weakening", "bearish_weakening"}
)
OPPOSITE_STRUCTURE: dict[str, str] = {
    "topping": "bottoming",
    "bottoming": "topping",
}
EXPECTED_SIDE: dict[str, str] = {
    "topping": "short",
    "bottoming": "long",
}
MIN_EVENTS_FOR_RECOMMENDATION = 5
MAE_WORSE_TOLERANCE_PCT = 0.15
REVERSAL_WORSE_TOLERANCE = 0.05


def _ts(v: object) -> pd.Timestamp:
    t = pd.Timestamp(v)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _iso(v: object | None) -> str | None:
    if v is None:
        return None
    return _ts(v).isoformat()


def _finite(v: object) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def resolve_variants(names: list[str]) -> list[tuple[str, WeakeningMultiBarMode]]:
    out: list[tuple[str, WeakeningMultiBarMode]] = []
    for name in names:
        key = name.strip().lower()
        if key not in VARIANT_ALIASES:
            raise ValueError(f"unknown variant {name!r}; expected loose or strict")
        out.append(VARIANT_ALIASES[key])
    if not out:
        raise ValueError("at least one variant required")
    return out


def target_enter_states(*, include_weakening: bool) -> frozenset[str]:
    if include_weakening:
        return CORE_ENTER_STATES | OPTIONAL_WEAKENING_STATES
    return CORE_ENTER_STATES


def config_for_variant(mode: WeakeningMultiBarMode):
    if mode == "off":
        return default_trend_state_config()
    return trend_state_config_c1(mode)


def build_price_arrays(frame: pd.DataFrame) -> dict[str, Any]:
    close = frame["close"].to_numpy(dtype=float)
    high = frame["high"].to_numpy(dtype=float)
    low = frame["low"].to_numpy(dtype=float)
    return {"close": close, "high": high, "low": low, "n_bars": int(len(frame))}


def compute_horizon_outcome(
    *,
    bar_index: int,
    horizon: int,
    reference_close: float,
    side: str | None,
    arrays: dict[str, Any],
) -> dict[str, Any]:
    """Vectorized forward metrics from bar_index+1 over ``horizon`` bars."""
    n = int(arrays["n_bars"])
    start = bar_index + 1
    end = start + horizon
    base: dict[str, Any] = {
        "horizon": horizon,
        "evaluable": False,
        "reason": None,
        "raw_close_return_pct": None,
        "directional_close_return_pct": None,
        "direction_hit": None,
        "mfe_pct": None,
        "mae_pct": None,
        "mfe_peak_offset": None,
        "mae_peak_offset": None,
        "bars_to_first_positive_directional": None,
        "mfe_mae_ratio": None,
    }
    if reference_close == 0.0 or not math.isfinite(reference_close):
        base["reason"] = "INVALID_REFERENCE_CLOSE"
        return base
    if end > n:
        base["reason"] = "INSUFFICIENT_FUTURE_CANDLES"
        base["available_future_bars"] = max(0, n - start)
        return base

    highs = arrays["high"][start:end]
    lows = arrays["low"][start:end]
    closes = arrays["close"][start:end]
    if not (np.isfinite(highs).all() and np.isfinite(lows).all() and np.isfinite(closes).all()):
        base["reason"] = "INVALID_OHLC_IN_FORWARD_WINDOW"
        return base

    end_close = float(closes[-1])
    raw = (end_close - reference_close) / abs(reference_close) * 100.0
    base["raw_close_return_pct"] = raw

    if side is not None:
        dret = directional_close_return_pct(
            side=side, reference_close=reference_close, future_close=end_close
        )
        base["directional_close_return_pct"] = dret
        base["direction_hit"] = bool(dret > 0.0)

        if side == "long":
            fav_path = (highs - reference_close) / abs(reference_close) * 100.0
            adv_path = (reference_close - lows) / abs(reference_close) * 100.0
            dir_path = (closes - reference_close) / abs(reference_close) * 100.0
        else:
            fav_path = (reference_close - lows) / abs(reference_close) * 100.0
            adv_path = (highs - reference_close) / abs(reference_close) * 100.0
            dir_path = (reference_close - closes) / abs(reference_close) * 100.0

        fav_path = np.maximum(fav_path, 0.0)
        adv_path = np.maximum(adv_path, 0.0)
        mfe = float(np.max(fav_path))
        mae = float(np.max(adv_path))
        mfe_idx = int(np.argmax(fav_path)) + 1
        mae_idx = int(np.argmax(adv_path)) + 1
        base["mfe_pct"] = mfe
        base["mae_pct"] = mae
        base["mfe_peak_offset"] = mfe_idx
        base["mae_peak_offset"] = mae_idx
        base["mfe_mae_ratio"] = mfe / mae if mae > 0.0 else None

        pos = np.where(dir_path > 0.0)[0]
        base["bars_to_first_positive_directional"] = (
            int(pos[0]) + 1 if len(pos) else None
        )
    else:
        base["directional_close_return_pct"] = None
        base["direction_hit"] = None

    base["evaluable"] = True
    return base


def state_duration_until_next_change(
    event_bar: int, new_state: str, state_by_bar: list[str]
) -> int | None:
    for j in range(event_bar + 1, len(state_by_bar)):
        if state_by_bar[j] != new_state:
            return j - event_bar
    return None


def fast_reversal_flags(
    *,
    event_bar: int,
    previous_state: str,
    new_state: str,
    state_by_bar: list[str],
) -> dict[str, bool]:
    opposite = OPPOSITE_STRUCTURE.get(new_state)
    out: dict[str, bool] = {}
    for w in FAST_REVERSAL_WINDOWS:
        segment = state_by_bar[event_bar + 1 : min(len(state_by_bar), event_bar + w + 1)]
        to_prev = previous_state in segment
        to_opp = opposite is not None and opposite in segment
        out[f"reversal_to_previous_within_{w}"] = to_prev
        out[f"reversal_to_opposite_within_{w}"] = to_opp
        out[f"fast_reversal_within_{w}"] = to_prev or to_opp
    return out


def _replay_core(
    *,
    frame: pd.DataFrame,
    mode: WeakeningMultiBarMode,
    variant_name: str,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    shared: SharedReplayContext | None,
    targets: frozenset[str],
) -> dict[str, Any]:
    cfg = config_for_variant(mode)
    rt = TrendRuntime()
    state_by_bar: list[str] = []
    events: list[dict[str, Any]] = []

    if shared is not None:
        bar_iter = shared.prepared_bars
        step_fn = lambda prep: step_trend_state_from_prepared(rt, prepared=prep, cfg=cfg)
    else:
        end_decision = _ts(frame["decision_time"].iloc[-1])
        install_htf_cache(frame, end_decision)
        scfg = default_regime_scanner_config().with_timeframe("5m")
        pivots = find_confirmed_pivots(frame, config=scfg)
        ohlcv = [
            c
            for c in ("timestamp", "open", "high", "low", "close", "volume")
            if c in frame.columns
        ]

        def _iter():
            for i, (_, row) in enumerate(frame.iterrows()):
                yield type(
                    "Prep",
                    (),
                    {
                        "bar_index": i,
                        "decision_time": _ts(row["decision_time"]),
                        "row": row.to_dict(),
                    },
                )()

        bar_iter = _iter()

        def step_fn(prep):  # type: ignore[no-untyped-def]
            nonlocal rt
            rt, snap, _ = step_trend_state(
                rt,
                candle_row=prep.row,
                pivots_5m=pivots,
                decision_time=prep.decision_time,
                candles_5m_as_of=frame.iloc[: prep.bar_index + 1][ohlcv],
                bar_index=prep.bar_index,
                cfg=cfg,
                scanner_cfg=scfg,
            )
            return rt, snap, _

    for prep in bar_iter:
        prev_state = rt.state
        if shared is not None:
            rt, snap, _ = step_fn(prep)
        else:
            rt, snap, _ = step_fn(prep)

        state_by_bar.append(snap.current_state)
        decision_ts = prep.decision_time
        if not (analyze_start <= decision_ts <= analyze_end):
            continue
        if snap.current_state == prev_state:
            continue
        if snap.current_state not in targets:
            continue

        row = prep.row
        events.append(
            {
                "symbol": None,
                "variant": variant_name,
                "mode": mode,
                "timestamp": _iso(decision_ts),
                "bar_index": int(prep.bar_index),
                "previous_state": prev_state,
                "new_state": snap.current_state,
                "entry_close": float(row["close"]),
                "trigger_reasons": "|".join(snap.active_reasons),
            }
        )

    return {
        "mode": mode,
        "variant": variant_name,
        "state_by_bar": state_by_bar,
        "events": events,
        "config": cfg.to_dict(),
    }


def enrich_events(
    *,
    symbol: str,
    replay_result: dict[str, Any],
    arrays: dict[str, Any],
    horizons: tuple[int, ...],
) -> list[dict[str, Any]]:
    state_by_bar: list[str] = replay_result["state_by_bar"]
    enriched: list[dict[str, Any]] = []

    for ev in replay_result["events"]:
        bar_i = int(ev["bar_index"])
        new_state = str(ev["new_state"])
        prev_state = str(ev["previous_state"])
        ref_close = float(ev["entry_close"])
        side = EXPECTED_SIDE.get(new_state)

        row = {**ev, "symbol": symbol}
        row["state_duration_bars"] = state_duration_until_next_change(
            bar_i, new_state, state_by_bar
        )
        row.update(
            fast_reversal_flags(
                event_bar=bar_i,
                previous_state=prev_state,
                new_state=new_state,
                state_by_bar=state_by_bar,
            )
        )

        for h in horizons:
            outcome = compute_horizon_outcome(
                bar_index=bar_i,
                horizon=int(h),
                reference_close=ref_close,
                side=side,
                arrays=arrays,
            )
            prefix = f"h{h}_"
            for key, val in outcome.items():
                if key == "horizon":
                    continue
                row[f"{prefix}{key}"] = val

        enriched.append(row)
    return enriched


def _median(vals: list[float]) -> float | None:
    return float(statistics.median(vals)) if vals else None


def _mean(vals: list[float]) -> float | None:
    return float(sum(vals) / len(vals)) if vals else None


def aggregate_by_state_horizon(
    events: list[dict[str, Any]],
    *,
    variant: str,
    horizons: tuple[int, ...],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    states = sorted({str(e["new_state"]) for e in events})

    for state in states:
        state_events = [e for e in events if e["new_state"] == state]
        for h in horizons:
            prefix = f"h{h}_"
            evaluable = [e for e in state_events if e.get(f"{prefix}evaluable") is True]
            incomplete = len(state_events) - len(evaluable)
            hits = [
                e[f"{prefix}direction_hit"]
                for e in evaluable
                if e.get(f"{prefix}direction_hit") is not None
            ]
            drets = [
                float(e[f"{prefix}directional_close_return_pct"])
                for e in evaluable
                if e.get(f"{prefix}directional_close_return_pct") is not None
            ]
            raws = [
                float(e[f"{prefix}raw_close_return_pct"])
                for e in evaluable
                if e.get(f"{prefix}raw_close_return_pct") is not None
            ]
            mfes = [
                float(e[f"{prefix}mfe_pct"])
                for e in evaluable
                if e.get(f"{prefix}mfe_pct") is not None
            ]
            maes = [
                float(e[f"{prefix}mae_pct"])
                for e in evaluable
                if e.get(f"{prefix}mae_pct") is not None
            ]
            ratios = [
                float(e[f"{prefix}mfe_mae_ratio"])
                for e in evaluable
                if e.get(f"{prefix}mfe_mae_ratio") is not None
            ]
            fast3 = [
                1.0 if e.get("fast_reversal_within_3") else 0.0 for e in state_events
            ]
            durations = [
                float(e["state_duration_bars"])
                for e in state_events
                if e.get("state_duration_bars") is not None
            ]

            rows.append(
                {
                    "variant": variant,
                    "new_state": state,
                    "horizon": h,
                    "n_events": len(state_events),
                    "n_evaluable": len(evaluable),
                    "n_incomplete_horizon": incomplete,
                    "hit_rate": (sum(1 for x in hits if x) / len(hits)) if hits else None,
                    "mean_directional_return": _mean(drets),
                    "median_directional_return": _median(drets),
                    "mean_raw_return": _mean(raws),
                    "median_raw_return": _median(raws),
                    "mean_mfe": _mean(mfes),
                    "median_mfe": _median(mfes),
                    "mean_mae": _mean(maes),
                    "median_mae": _median(maes),
                    "mean_mfe_mae_ratio": _mean(ratios),
                    "median_mfe_mae_ratio": _median(ratios),
                    "fast_reversal_rate_within_3": _mean(fast3),
                    "fast_reversal_rate_within_6": _mean(
                        [1.0 if e.get("fast_reversal_within_6") else 0.0 for e in state_events]
                    ),
                    "fast_reversal_rate_within_12": _mean(
                        [1.0 if e.get("fast_reversal_within_12") else 0.0 for e in state_events]
                    ),
                    "fast_reversal_rate_within_24": _mean(
                        [1.0 if e.get("fast_reversal_within_24") else 0.0 for e in state_events]
                    ),
                    "median_state_duration_bars": _median(durations),
                }
            )
    return rows


def build_comparison(
    loose_agg: list[dict[str, Any]],
    strict_agg: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    loose_map = {(r["new_state"], r["horizon"]): r for r in loose_agg}
    strict_map = {(r["new_state"], r["horizon"]): r for r in strict_agg}
    keys = sorted(set(loose_map) | set(strict_map))

    rows: list[dict[str, Any]] = []
    for state, horizon in keys:
        lo = loose_map.get((state, horizon), {})
        st = strict_map.get((state, horizon), {})
        row: dict[str, Any] = {
            "new_state": state,
            "horizon": horizon,
            "loose_n_events": lo.get("n_events"),
            "strict_n_events": st.get("n_events"),
            "loose_hit_rate": lo.get("hit_rate"),
            "strict_hit_rate": st.get("hit_rate"),
            "hit_rate_delta_loose_minus_strict": None,
            "loose_mean_directional_return": lo.get("mean_directional_return"),
            "strict_mean_directional_return": st.get("mean_directional_return"),
            "directional_return_delta_loose_minus_strict": None,
            "loose_mean_mae": lo.get("mean_mae"),
            "strict_mean_mae": st.get("mean_mae"),
            "loose_fast_reversal_rate_within_3": lo.get("fast_reversal_rate_within_3"),
            "strict_fast_reversal_rate_within_3": st.get("fast_reversal_rate_within_3"),
            "loose_median_state_duration_bars": lo.get("median_state_duration_bars"),
            "strict_median_state_duration_bars": st.get("median_state_duration_bars"),
        }
        if lo.get("hit_rate") is not None and st.get("hit_rate") is not None:
            row["hit_rate_delta_loose_minus_strict"] = float(lo["hit_rate"]) - float(
                st["hit_rate"]
            )
        if lo.get("mean_directional_return") is not None and st.get(
            "mean_directional_return"
        ) is not None:
            row["directional_return_delta_loose_minus_strict"] = float(
                lo["mean_directional_return"]
            ) - float(st["mean_directional_return"])
        rows.append(row)
    return rows


def recommend_variant(
    comparison: list[dict[str, Any]],
    *,
    horizons: tuple[int, ...],
) -> dict[str, Any]:
    """Cautious recommendation from measured outcomes only."""
    primary_h = 12 if 12 in horizons else horizons[len(horizons) // 2]
    core_states = ("topping", "bottoming")

    loose_score = 0
    strict_score = 0
    reasons: list[str] = []
    insufficient = False

    for state in core_states:
        row = next(
            (r for r in comparison if r["new_state"] == state and r["horizon"] == primary_h),
            None,
        )
        if row is None:
            insufficient = True
            reasons.append(f"missing comparison for {state}@{primary_h}")
            continue

        n_lo = int(row.get("loose_n_events") or 0)
        n_st = int(row.get("strict_n_events") or 0)
        if n_lo < MIN_EVENTS_FOR_RECOMMENDATION or n_st < MIN_EVENTS_FOR_RECOMMENDATION:
            insufficient = True
            reasons.append(
                f"insufficient events for {state}: loose={n_lo} strict={n_st}"
            )
            continue

        hit_lo = row.get("loose_hit_rate")
        hit_st = row.get("strict_hit_rate")
        ret_lo = row.get("loose_mean_directional_return")
        ret_st = row.get("strict_mean_directional_return")
        mae_lo = row.get("loose_mean_mae")
        mae_st = row.get("strict_mean_mae")
        rev_lo = row.get("loose_fast_reversal_rate_within_3")
        rev_st = row.get("strict_fast_reversal_rate_within_3")

        if hit_lo is not None and hit_st is not None:
            if hit_lo > hit_st + 0.02:
                loose_score += 1
            elif hit_st > hit_lo + 0.02:
                strict_score += 1

        if ret_lo is not None and ret_st is not None:
            if ret_lo > ret_st + 0.05:
                loose_score += 1
            elif ret_st > ret_lo + 0.05:
                strict_score += 1

        if mae_lo is not None and mae_st is not None and mae_lo > 0:
            if mae_st > mae_lo * (1.0 + MAE_WORSE_TOLERANCE_PCT):
                loose_score += 1
                reasons.append(f"{state}: strict MAE worse vs loose")
            elif mae_lo > mae_st * (1.0 + MAE_WORSE_TOLERANCE_PCT):
                strict_score += 1
                reasons.append(f"{state}: loose MAE worse vs strict")

        if rev_lo is not None and rev_st is not None:
            if rev_lo + REVERSAL_WORSE_TOLERANCE < rev_st:
                strict_score -= 1
                reasons.append(f"{state}: strict more fast reversals")
            elif rev_st + REVERSAL_WORSE_TOLERANCE < rev_lo:
                loose_score -= 1
                reasons.append(f"{state}: loose more fast reversals")

    if insufficient:
        choice = "inconclusive"
    elif loose_score > strict_score:
        choice = "loose"
    elif strict_score > loose_score:
        choice = "strict"
    else:
        choice = "inconclusive"

    return {
        "recommendation": choice,
        "primary_horizon": primary_h,
        "loose_score": loose_score,
        "strict_score": strict_score,
        "reasons": reasons,
        "production_default_remains": "off (C1-A baseline)",
        "note": (
            "topping/bottoming are context states, not confirmed trends; "
            "weakening states are not auto trade signals"
        ),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    pd.DataFrame(rows).to_csv(path, index=False)


def replay_variant_optimized(
    frame: pd.DataFrame,
    *,
    mode: WeakeningMultiBarMode,
    variant_name: str,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    shared: SharedReplayContext,
    targets: frozenset[str],
) -> dict[str, Any]:
    shared_replay_mod.VARIANT_POLICY_REPLAY_COUNT += 1
    return _replay_core(
        frame=frame,
        mode=mode,
        variant_name=variant_name,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        shared=shared,
        targets=targets,
    )


def replay_variant_naive(
    frame: pd.DataFrame,
    *,
    mode: WeakeningMultiBarMode,
    variant_name: str,
    analyze_start: pd.Timestamp,
    analyze_end: pd.Timestamp,
    targets: frozenset[str],
) -> dict[str, Any]:
    return _replay_core(
        frame=frame,
        mode=mode,
        variant_name=variant_name,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        shared=None,
        targets=targets,
    )


def _event_signature(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "bar_index": e["bar_index"],
            "new_state": e["new_state"],
            "previous_state": e["previous_state"],
            "timestamp": e["timestamp"],
        }
        for e in events
    ]


def run_audit(
    *,
    symbol: str = "APTUSDT",
    output_dir: Path = DEFAULT_OUT,
    load_start: str = LOAD_START,
    load_end: str = LOAD_END,
    analyze_start: str = ANALYZE_START,
    analyze_end: str = ANALYZE_END,
    variants: list[str] | None = None,
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
    include_weakening: bool = True,
) -> dict[str, Any]:
    assert_safe_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.perf_counter()
    reset_audit_counters()
    variant_list = resolve_variants(variants or ["loose", "strict"])
    targets = target_enter_states(include_weakening=include_weakening)

    frame = load_analysis_frame(symbol, load_start=load_start, load_end=load_end)
    a0 = _ts(analyze_start)
    a1 = _ts(analyze_end)
    arrays = build_price_arrays(frame)

    shared = load_or_build_shared_context(frame, cache_dir=output_dir / ".cache")

    all_events: list[dict[str, Any]] = []
    aggregates: dict[str, list[dict[str, Any]]] = {}
    replay_meta: dict[str, Any] = {}
    replay_by_variant: dict[str, dict[str, Any]] = {}
    events_by_variant: dict[str, list[dict[str, Any]]] = {}

    for variant_name, mode in variant_list:
        replay = replay_variant_optimized(
            frame,
            mode=mode,
            variant_name=variant_name,
            analyze_start=a0,
            analyze_end=a1,
            shared=shared,
            targets=targets,
        )
        replay_by_variant[variant_name] = replay
        events = enrich_events(
            symbol=symbol, replay_result=replay, arrays=arrays, horizons=horizons
        )
        events_by_variant[variant_name] = events
        all_events.extend(events)
        aggregates[variant_name] = aggregate_by_state_horizon(
            events, variant=variant_name, horizons=horizons
        )
        replay_meta[variant_name] = {
            "n_events": len(events),
            "events_by_state": {
                st: sum(1 for e in events if e["new_state"] == st)
                for st in sorted(targets)
            },
        }

    comparison: list[dict[str, Any]] = []
    if "C1_B_loose" in aggregates and "C1_C_strict" in aggregates:
        comparison = build_comparison(
            aggregates["C1_B_loose"], aggregates["C1_C_strict"]
        )

    recommendation = recommend_variant(comparison, horizons=horizons)
    elapsed = time.perf_counter() - t0

    write_csv(output_dir / "events.csv", all_events)
    write_csv(
        output_dir / "aggregate_by_state_horizon.csv",
        [r for rows in aggregates.values() for r in rows],
    )
    write_csv(output_dir / "comparison.csv", comparison)

    rec_key = {
        "loose": "C1_B_loose",
        "strict": "C1_C_strict",
    }.get(str(recommendation.get("recommendation") or ""))

    pine_export = export_audit_pine_artifacts(
        output_dir=output_dir,
        phase="C2_forward_outcome",
        symbol=symbol,
        analyze_start=analyze_start,
        analyze_end=analyze_end,
        variants={
            variant_name: {
                "timeline_rows": build_timeline_from_state_series(
                    frame,
                    replay_by_variant[variant_name]["state_by_bar"],
                    analyze_start=a0,
                    analyze_end=a1,
                ),
                "marker_rows": marker_rows_from_events(
                    events_by_variant[variant_name],
                    label_field="new_state",
                ),
            }
            for variant_name in replay_by_variant
        },
        recommended_variant=rec_key,
    )

    metadata = {
        "phase": "C2_forward_outcome",
        "symbol": symbol,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "variants": [v for v, _ in variant_list],
        "horizons": list(horizons),
        "include_weakening": include_weakening,
        "target_states": sorted(targets),
        "n_load_bars": int(len(frame)),
        "shared_cache_key": shared.cache_key,
        "performance": {
            "elapsed_seconds": round(elapsed, 3),
            "shared_structure_passes": shared.structure_pass_count,
            "variant_policy_replays": shared_replay_mod.VARIANT_POLICY_REPLAY_COUNT,
        },
        "safety": {
            "policy_unchanged": True,
            "production_default_off": True,
            "no_live_bot_changes": True,
        },
        "pine_export": pine_export,
    }
    (output_dir / "metadata.json").write_text(
        json.dumps(json_safe(metadata), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    summary_core = {
        "phase": "C2_forward_outcome",
        "symbol": symbol,
        "load_start": load_start,
        "load_end": load_end,
        "analyze_start": analyze_start,
        "analyze_end": analyze_end,
        "horizons": list(horizons),
        "variants": replay_meta,
        "aggregates": aggregates,
        "comparison": comparison,
        "recommendation": recommendation,
        "n_total_events": len(all_events),
        "n_incomplete_horizon_events": sum(
            1
            for e in all_events
            for h in horizons
            if e.get(f"h{h}_evaluable") is False
            and e.get(f"h{h}_reason") == "INSUFFICIENT_FUTURE_CANDLES"
        ),
    }
    blob = json.dumps(json_safe(summary_core), sort_keys=True, separators=(",", ":"))
    summary = {
        **summary_core,
        "performance": metadata["performance"],
        "pine_export": {
            "recommended_pine": pine_export.get("recommended_pine"),
            "metadata_path": pine_export.get("metadata_path"),
            "variants": {
                k: v.get("pine_path") for k, v in (pine_export.get("variants") or {}).items()
            },
        },
        "deterministic_hash": hashlib.sha256(blob.encode()).hexdigest(),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(json_safe(summary), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Phase C2 trend-state forward outcome audit (loose vs strict)"
    )
    p.add_argument("--symbol", default="APTUSDT")
    p.add_argument("--output-dir", type=Path, default=DEFAULT_OUT)
    p.add_argument("--load-start", default=LOAD_START)
    p.add_argument("--load-end", default=LOAD_END)
    p.add_argument("--analyze-start", default=ANALYZE_START)
    p.add_argument("--analyze-end", default=ANALYZE_END)
    p.add_argument(
        "--variants",
        nargs="+",
        default=["loose", "strict"],
        help="loose and/or strict (C1-B / C1-C)",
    )
    p.add_argument(
        "--horizons",
        nargs="+",
        type=int,
        default=list(DEFAULT_HORIZONS),
        help="forward horizons in 5m bars",
    )
    p.add_argument(
        "--no-weakening",
        action="store_true",
        help="exclude bullish_weakening / bearish_weakening entry events",
    )
    args = p.parse_args(argv)
    horizons = tuple(int(h) for h in args.horizons)
    summary = run_audit(
        symbol=args.symbol,
        output_dir=args.output_dir,
        load_start=args.load_start,
        load_end=args.load_end,
        analyze_start=args.analyze_start,
        analyze_end=args.analyze_end,
        variants=args.variants,
        horizons=horizons,
        include_weakening=not args.no_weakening,
    )
    print(
        json.dumps(
            {
                "hash": summary["deterministic_hash"],
                "recommendation": summary["recommendation"],
                "variants": summary["variants"],
                "n_total_events": summary["n_total_events"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
