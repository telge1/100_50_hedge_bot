"""Orchestrate APT fractal cycle wave efficiency analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis import (
    ALL_TFS,
    AUDIT_VERSION,
    PARENT_TFS,
    SYMBOL_PRIMARY,
    VISIBLE_MIN_ABS_MEAN_MOVE,
    VISIBLE_MIN_TFS_WITH_SIGN,
    WAVE_TFS,
    WEAK_MIN_TFS_WITH_SIGN,
)
from orderbook_analyse.fractal_cycle_wave_analysis.alignment import (
    annotate_alignment,
    default_child_parent_pairs,
    re_alignment_sequences,
    summarize_alignment,
)
from orderbook_analyse.fractal_cycle_wave_analysis.indicators import attach_indicators
from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import (
    coverage_audit,
    full_stack_window,
    load_mysql_ohlcv_tf,
)
from orderbook_analyse.fractal_cycle_wave_analysis.waves import (
    segment_stoch_waves,
    summarize_tf_waves,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import DEFAULT_ENV_FILE


def decide_visibility(tf_summaries: list[dict[str, Any]]) -> dict[str, Any]:
    coherent = []
    abs_means = []
    for s in tf_summaries:
        asym = s.get("asymmetry") or {}
        if asym.get("directionally_coherent"):
            coherent.append(s["timeframe"])
            up_m = abs(float(asym.get("signed_up_mean") or 0.0))
            dn_m = abs(float(asym.get("signed_down_mean") or 0.0))
            abs_means.append(0.5 * (up_m + dn_m))
    n_coh = len(coherent)
    med_abs = float(pd.Series(abs_means).median()) if abs_means else 0.0
    if n_coh >= VISIBLE_MIN_TFS_WITH_SIGN and med_abs >= VISIBLE_MIN_ABS_MEAN_MOVE:
        decision = "FRACTAL_WAVE_EFFICIENCY_VISIBLE"
    elif n_coh >= WEAK_MIN_TFS_WITH_SIGN:
        decision = "FRACTAL_WAVE_EFFICIENCY_WEAK"
    else:
        decision = "FRACTAL_WAVE_EFFICIENCY_NOT_VISIBLE"
    return {
        "decision": decision,
        "n_coherent_tfs": n_coh,
        "coherent_tfs": coherent,
        "median_abs_mean_move_pct": med_abs,
        "thresholds": {
            "VISIBLE_MIN_TFS_WITH_SIGN": VISIBLE_MIN_TFS_WITH_SIGN,
            "VISIBLE_MIN_ABS_MEAN_MOVE": VISIBLE_MIN_ABS_MEAN_MOVE,
            "WEAK_MIN_TFS_WITH_SIGN": WEAK_MIN_TFS_WITH_SIGN,
        },
    }


def run_symbol_analysis(
    *,
    symbol: str = SYMBOL_PRIMARY,
    env_file: Path = DEFAULT_ENV_FILE,
    timeframes: tuple[str, ...] = ALL_TFS,
) -> dict[str, Any]:
    coverage = coverage_audit(symbol=symbol, env_file=env_file, timeframes=timeframes)
    window = full_stack_window(coverage, timeframes)

    indicators: dict[str, pd.DataFrame] = {}
    waves_by_tf: dict[str, pd.DataFrame] = {}
    tf_summaries: list[dict[str, Any]] = []

    # Process HTF first so partial progress is useful; 1m last (largest).
    process_order = tuple(tf for tf in ("1M", "1w", "1d", "4h", "1h", "30m", "15m", "5m", "1m") if tf in timeframes)
    for missing in timeframes:
        if missing not in process_order:
            process_order = process_order + (missing,)

    for tf in process_order:
        raw = load_mysql_ohlcv_tf(symbol=symbol, timeframe=tf, env_file=env_file)
        if raw.empty:
            tf_summaries.append(summarize_tf_waves(pd.DataFrame(), timeframe=tf))
            continue
        print(f"[ind] {symbol} {tf} …", flush=True)
        ind = attach_indicators(raw)
        del raw
        print(f"[waves] {symbol} {tf} …", flush=True)
        waves = segment_stoch_waves(ind)
        waves["symbol"] = symbol
        waves["timeframe"] = tf
        waves_by_tf[tf] = waves
        # Keep slim parent lookup only (direction at available_at).
        indicators[tf] = ind[["available_at", "stoch_dir"]].copy()
        del ind
        tf_summaries.append(summarize_tf_waves(waves, timeframe=tf))
        print(
            f"[waves] {symbol} {tf}: n_waves={len(waves)} "
            f"up={(waves['direction']=='UP').sum() if len(waves) else 0} "
            f"down={(waves['direction']=='DOWN').sum() if len(waves) else 0}",
            flush=True,
        )

    # Cross-TF alignment
    alignment_reports: list[dict[str, Any]] = []
    realign_reports: list[dict[str, Any]] = []
    for child_tf, parent_tf in default_child_parent_pairs():
        if child_tf not in waves_by_tf or parent_tf not in indicators:
            continue
        aligned = annotate_alignment(
            waves_by_tf[child_tf],
            child_tf=child_tf,
            parent_tf=parent_tf,
            parent_ind=indicators[parent_tf],
        )
        alignment_reports.append(
            {
                "child_tf": child_tf,
                "parent_tf": parent_tf,
                "summary": summarize_alignment(aligned),
            }
        )
        realign_reports.append(
            {
                "child_tf": child_tf,
                "parent_tf": parent_tf,
                "sequences": re_alignment_sequences(aligned),
            }
        )

    # Parent-cycle context efficiency: wave TFs conditioned on 1d direction
    parent_ctx: dict[str, Any] = {}
    if "1d" in indicators:
        for tf in WAVE_TFS:
            if tf not in waves_by_tf:
                continue
            aligned = annotate_alignment(
                waves_by_tf[tf],
                child_tf=tf,
                parent_tf="1d",
                parent_ind=indicators["1d"],
            )
            parent_ctx[tf] = summarize_alignment(aligned)

    visibility = decide_visibility(tf_summaries)

    # RSI context: DOWN waves with RSI still >50 → inefficient bearish control hint
    rsi_ctx: dict[str, Any] = {}
    for tf, waves in waves_by_tf.items():
        if waves.empty:
            continue
        down = waves[waves["direction"] == "DOWN"]
        up = waves[waves["direction"] == "UP"]
        rsi_ctx[tf] = {
            "down_with_rsi_gt50": {
                "n": int(((down["rsi_end_gt_50"] == True)).sum()) if len(down) else 0,
                "mean_price_move_pct": float(
                    down.loc[down["rsi_end_gt_50"] == True, "price_move_pct"].mean()
                )
                if len(down) and (down["rsi_end_gt_50"] == True).any()
                else None,
            },
            "up_with_rsi_lt50": {
                "n": int(((up["rsi_end_lt_50"] == True)).sum()) if len(up) else 0,
                "mean_price_move_pct": float(
                    up.loc[up["rsi_end_lt_50"] == True, "price_move_pct"].mean()
                )
                if len(up) and (up["rsi_end_lt_50"] == True).any()
                else None,
            },
        }

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": symbol,
        "coverage": coverage,
        "full_stack_window": window,
        "parent_tfs": list(PARENT_TFS),
        "wave_tfs": list(WAVE_TFS),
        "tf_summaries": tf_summaries,
        "rsi_context": rsi_ctx,
        "alignment": alignment_reports,
        "re_alignment": realign_reports,
        "parent_1d_context": parent_ctx,
        "visibility": visibility,
        "waves_by_tf": waves_by_tf,
        "n_indicator_bars": {tf: int(len(df)) for tf, df in indicators.items()},
    }
