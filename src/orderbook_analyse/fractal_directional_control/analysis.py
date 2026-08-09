"""Orchestrate directional control + CCI turn analysis from wave CSVs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_directional_control import (
    AUDIT_VERSION,
    CCI_BUCKETS,
    MIN_SAMPLE_MARK,
    SYMBOL,
    TRIGGER_TFS,
)
from orderbook_analyse.fractal_directional_control.flags import (
    bear_reversal_candidate,
    bull_reversal_candidate,
    cci_bucket,
    cci_extreme_for_wave,
    inefficient_down_in_bull,
    inefficient_up_in_bear,
    realign_bear_setup,
    realign_bull_setup,
    summarize_next_moves,
)
from orderbook_analyse.fractal_directional_control.load_join import (
    DEFAULT_WAVE_DIR,
    attach_next_opposite_wave,
    join_trigger_context,
    load_all_waves,
)

# Compact columns for joined export (avoid mega dumps).
JOINED_KEEP = [
    "timeframe",
    "direction",
    "start_available_at",
    "end_available_at",
    "signed_price_move_pct",
    "price_move_pct",
    "directional_efficiency",
    "favorable_move_pct",
    "adverse_move_pct",
    "rsi_end",
    "rsi_end_gt_50",
    "rsi_end_lt_50",
    "rsi_gt50_share",
    "price_vs_ema20_end",
    "ema9_vs_ema20_end",
    "cci_start",
    "cci_end",
    "cci_delta",
    "cci_min",
    "cci_max",
    "cci_strongest_pos",
    "cci_strongest_neg",
    "stoch_zone_end",
    "d1_direction",
    "d1_stoch_zone_end",
    "d1_rsi_end",
    "d1_rsi_end_gt_50",
    "d1_ema9_vs_ema20_end",
    "d1_directional_efficiency",
    "w1_direction",
    "w1_stoch_zone_end",
    "w1_rsi_end",
    "w1_ema9_vs_ema20_end",
    "w1_directional_efficiency",
    "m1_direction",
    "m1_available",
    "h4_direction",
    "h4_stoch_zone_end",
    "h4_rsi_end",
    "h4_rsi_gt50_share",
    "h4_price_vs_ema20_end",
    "h4_ema9_vs_ema20_end",
    "h4_directional_efficiency",
    "h1_direction",
    "h1_stoch_zone_end",
    "h1_rsi_end",
    "h1_rsi_gt50_share",
    "h1_price_vs_ema20_end",
    "h1_ema9_vs_ema20_end",
    "h1_directional_efficiency",
    "bull_control_setup",
    "bear_control_setup",
    "next_opp_signed_price_move_pct",
    "has_next_opp",
]


def _baseline_next(df: pd.DataFrame, direction: str, timeframe: str, label: str) -> dict:
    sub = df[df["direction"] == direction]
    return summarize_next_moves(sub, label=label, timeframe=timeframe)


def decide_directional(control_rows: list[dict], realign_rows: list[dict]) -> str:
    """Primary decision from fixed comparisons vs baseline (no tuning)."""
    # Compare setup next-move median vs same-TF baseline opposite wave median
    edges = []
    for row in control_rows:
        if row.get("n", 0) < MIN_SAMPLE_MARK:
            continue
        base = row.get("baseline_median_next_signed")
        med = row.get("median_next_signed_price_move_pct")
        if base is None or med is None:
            continue
        # for bull control we look at next UP; expect med > base
        # for bear control next DOWN; expect med > base (signed for DOWN is positive when falls)
        edges.append(med - base)

    realign_ok = 0
    realign_n = 0
    for row in realign_rows:
        if row.get("n", 0) < MIN_SAMPLE_MARK:
            continue
        realign_n += 1
        base = row.get("baseline_median_next_signed")
        med = row.get("median_next_signed_price_move_pct")
        if base is not None and med is not None and med > base:
            realign_ok += 1

    pos_edges = sum(1 for e in edges if e > 0)
    if len(edges) >= 4 and pos_edges >= max(3, int(0.7 * len(edges))) and (
        realign_n == 0 or realign_ok / realign_n >= 0.5
    ):
        return "DIRECTIONAL_CONTROL_SIGNAL_VISIBLE"
    if len(edges) >= 2 and pos_edges >= 1:
        return "DIRECTIONAL_CONTROL_CONTEXT_DEPENDENT"
    return "DIRECTIONAL_CONTROL_NOT_ROBUST"


def decide_cci(cci_rows: list[dict], combo_rows: list[dict]) -> str:
    """CCI turn value: higher extreme buckets should show stronger next opposite moves."""
    # Per TF: compare gt300 / 200_300 median vs lt100 median for same end direction
    lifts = []
    for tf in TRIGGER_TFS:
        low = [r for r in cci_rows if r.get("timeframe") == tf and r.get("cci_bucket") == "lt100"]
        high = [
            r
            for r in cci_rows
            if r.get("timeframe") == tf and r.get("cci_bucket") in ("200_300", "gt300")
        ]
        if not low or not high:
            continue
        low_med = np.nanmean([r["median_next_signed_price_move_pct"] for r in low if r.get("n", 0) >= 20])
        high_vals = [r["median_next_signed_price_move_pct"] for r in high if r.get("n", 0) >= 20]
        if not high_vals or not np.isfinite(low_med):
            continue
        high_med = float(np.nanmean(high_vals))
        if np.isfinite(high_med):
            lifts.append(high_med - float(low_med))

    combo_lifts = []
    for r in combo_rows:
        if r.get("n", 0) < MIN_SAMPLE_MARK or r.get("n_without", 0) < MIN_SAMPLE_MARK:
            continue
        if r.get("median_with") is None or r.get("median_without") is None:
            continue
        combo_lifts.append(r["median_with"] - r["median_without"])

    strong = sum(1 for x in lifts if x > 0)
    combo_pos = sum(1 for x in combo_lifts if x > 0)
    if len(lifts) >= 2 and strong >= max(2, int(0.66 * len(lifts))) and (
        not combo_lifts or combo_pos >= max(1, int(0.5 * len(combo_lifts)))
    ):
        return "CCI_TURN_VALUE_VISIBLE"
    if (lifts and strong >= 1) or (combo_lifts and combo_pos >= 1):
        return "CCI_TURN_VALUE_WEAK"
    return "CCI_TURN_VALUE_NOT_VISIBLE"


def run_analysis(wave_dir: Path = DEFAULT_WAVE_DIR) -> dict[str, Any]:
    print(f"[load] waves from {wave_dir}", flush=True)
    waves = load_all_waves(wave_dir)

    control_rows: list[dict] = []
    realign_rows: list[dict] = []
    cci_rows: list[dict] = []
    combo_rows: list[dict] = []
    joined_parts: list[pd.DataFrame] = []

    for tf in TRIGGER_TFS:
        print(f"[join] {tf}", flush=True)
        base = waves[tf]
        joined = join_trigger_context(base, waves, decision_col="end_available_at")
        joined = attach_next_opposite_wave(joined)

        joined["bull_control_setup"] = inefficient_down_in_bull(joined)
        joined["bear_control_setup"] = inefficient_up_in_bear(joined)
        joined["realign_bear_setup"] = realign_bear_setup(joined)
        joined["realign_bull_setup"] = realign_bull_setup(joined)
        joined["cci_extreme_abs"] = cci_extreme_for_wave(joined)
        joined["cci_bucket"] = cci_bucket(joined["cci_extreme_abs"])

        # baselines: next opposite after any UP / any DOWN
        base_up = _baseline_next(joined, "UP", tf, "baseline_after_any_UP")
        base_down = _baseline_next(joined, "DOWN", tf, "baseline_after_any_DOWN")

        # A) BULL CONTROL → next UP
        bull_setup = joined[joined["bull_control_setup"] & (joined["direction"] == "DOWN")]
        bull_stats = summarize_next_moves(bull_setup, label="BULL_CONTROL_next_UP", timeframe=tf)
        # next opp after DOWN should be UP; signed of UP = price_move
        bull_stats["baseline_median_next_signed"] = base_down["median_next_signed_price_move_pct"]
        bull_stats["baseline_mean_next_signed"] = base_down["mean_next_signed_price_move_pct"]
        bull_stats["edge_vs_baseline_median"] = (
            None
            if bull_stats["median_next_signed_price_move_pct"] is None
            or base_down["median_next_signed_price_move_pct"] is None
            else bull_stats["median_next_signed_price_move_pct"]
            - base_down["median_next_signed_price_move_pct"]
        )
        control_rows.append(bull_stats)

        # B) BEAR CONTROL → next DOWN
        bear_setup = joined[joined["bear_control_setup"] & (joined["direction"] == "UP")]
        bear_stats = summarize_next_moves(bear_setup, label="BEAR_CONTROL_next_DOWN", timeframe=tf)
        bear_stats["baseline_median_next_signed"] = base_up["median_next_signed_price_move_pct"]
        bear_stats["baseline_mean_next_signed"] = base_up["mean_next_signed_price_move_pct"]
        bear_stats["edge_vs_baseline_median"] = (
            None
            if bear_stats["median_next_signed_price_move_pct"] is None
            or base_up["median_next_signed_price_move_pct"] is None
            else bear_stats["median_next_signed_price_move_pct"]
            - base_up["median_next_signed_price_move_pct"]
        )
        control_rows.append(bear_stats)

        # Re-alignment
        rb = joined[joined["realign_bear_setup"]]
        rb_stats = summarize_next_moves(rb, label="REALIGN_BEAR_next_DOWN", timeframe=tf)
        rb_stats["baseline_median_next_signed"] = base_up["median_next_signed_price_move_pct"]
        rb_stats["edge_vs_baseline_median"] = (
            None
            if rb_stats["median_next_signed_price_move_pct"] is None
            or base_up["median_next_signed_price_move_pct"] is None
            else rb_stats["median_next_signed_price_move_pct"]
            - base_up["median_next_signed_price_move_pct"]
        )
        realign_rows.append(rb_stats)

        ru = joined[joined["realign_bull_setup"]]
        ru_stats = summarize_next_moves(ru, label="REALIGN_BULL_next_UP", timeframe=tf)
        ru_stats["baseline_median_next_signed"] = base_down["median_next_signed_price_move_pct"]
        ru_stats["edge_vs_baseline_median"] = (
            None
            if ru_stats["median_next_signed_price_move_pct"] is None
            or base_down["median_next_signed_price_move_pct"] is None
            else ru_stats["median_next_signed_price_move_pct"]
            - base_down["median_next_signed_price_move_pct"]
        )
        realign_rows.append(ru_stats)

        # CCI turn buckets by ending wave direction
        for end_dir in ("UP", "DOWN"):
            sub = joined[joined["direction"] == end_dir]
            for bname, lo, hi in CCI_BUCKETS:
                bucket = sub[sub["cci_bucket"] == bname]
                stats = summarize_next_moves(
                    bucket, label=f"CCI_TURN_after_{end_dir}", timeframe=tf
                )
                stats["cci_bucket"] = bname
                stats["end_direction"] = end_dir
                stats["bucket_lo"] = lo
                stats["bucket_hi"] = hi if np.isfinite(hi) else None
                cci_rows.append(stats)

        # CCI + wave failure combo
        for name, with_cci, without_extra in (
            (
                "BEAR_REVERSAL_CANDIDATE",
                bear_reversal_candidate(joined, require_strong_cci=True),
                bear_reversal_candidate(joined, require_strong_cci=False)
                & ~(joined["cci_strongest_pos"].astype(float) >= 150.0),
            ),
            (
                "BULL_REVERSAL_CANDIDATE",
                bull_reversal_candidate(joined, require_strong_cci=True),
                bull_reversal_candidate(joined, require_strong_cci=False)
                & ~(joined["cci_strongest_neg"].astype(float).abs() >= 150.0),
            ),
        ):
            w = joined[with_cci]
            wo = joined[without_extra]
            sw = summarize_next_moves(w, label=f"{name}_with_CCI", timeframe=tf)
            swo = summarize_next_moves(wo, label=f"{name}_without_strong_CCI", timeframe=tf)
            combo_rows.append(
                {
                    "label": name,
                    "timeframe": tf,
                    "n": sw["n"],
                    "n_setup": sw["n_setup"],
                    "n_without": swo["n"],
                    "n_without_setup": swo["n_setup"],
                    "small_sample": sw["n"] < MIN_SAMPLE_MARK or swo["n"] < MIN_SAMPLE_MARK,
                    "median_with": sw["median_next_signed_price_move_pct"],
                    "mean_with": sw["mean_next_signed_price_move_pct"],
                    "median_without": swo["median_next_signed_price_move_pct"],
                    "mean_without": swo["mean_next_signed_price_move_pct"],
                    "lift_median": (
                        None
                        if sw["median_next_signed_price_move_pct"] is None
                        or swo["median_next_signed_price_move_pct"] is None
                        else sw["median_next_signed_price_move_pct"]
                        - swo["median_next_signed_price_move_pct"]
                    ),
                    "share_next_pos_with": sw["share_next_signed_positive"],
                    "share_next_pos_without": swo["share_next_signed_positive"],
                }
            )

        keep_cols = [c for c in JOINED_KEEP if c in joined.columns]
        # For 1m keep a stratified sample to avoid huge dump? User asked for joined file.
        # Write all triggers but compact columns only.
        part = joined[keep_cols].copy()
        if tf == "1m":
            # downsample 1m joined to every 10th row for dump size; full stats already computed
            part = part.iloc[::10].copy()
            part["note"] = "1m_joined_decimated_10x_for_size"
        joined_parts.append(part)
        print(
            f"[done] {tf}: bull_n={int(joined['bull_control_setup'].sum())} "
            f"bear_n={int(joined['bear_control_setup'].sum())}",
            flush=True,
        )

    directional = decide_directional(control_rows, realign_rows)
    cci_dec = decide_cci(cci_rows, combo_rows)

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "wave_dir": str(wave_dir),
        "directional_control_summary": control_rows,
        "realignment_results": realign_rows,
        "cci_turn_results": cci_rows,
        "cci_wave_failure_results": combo_rows,
        "joined_trigger_context": pd.concat(joined_parts, ignore_index=True),
        "decisions": {
            "directional_control": directional,
            "cci_turn": cci_dec,
        },
        "method_notes": {
            "causality": "parent wave end_available_at <= trigger end_available_at",
            "no_threshold_search": True,
            "cci_strong_fixed": 150.0,
            "weak_price_abs_pct": 0.02,
            "1m_joined_decimated": "10x for dump size; all stats use full 1m",
            "1w_1M": "context only; decisions do not depend on their sample size",
        },
    }
