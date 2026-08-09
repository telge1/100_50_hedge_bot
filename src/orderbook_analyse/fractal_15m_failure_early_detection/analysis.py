"""Orchestrate early 15m-failure detection analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_15m_failure_early_detection import (
    AUDIT_VERSION,
    LEAD_BUCKETS,
    METHOD_DOC,
    MIN_SAMPLE,
    PERSIST_BUCKETS,
    SNAPSHOT_OFFSETS_MIN,
    SYMBOL,
)
from orderbook_analyse.fractal_15m_failure_early_detection.outcomes import (
    attach_forward,
    load_5m,
    metrics,
)
from orderbook_analyse.fractal_15m_failure_early_detection.snapshots import (
    build_snapshots,
    load_failure_labels,
    load_micro_waves,
    load_waves_15m,
    prepare_ohlcv,
)


def prediction_stats(sub: pd.DataFrame, *, direction: str, offset: int) -> dict:
    """Precision/recall for early candidate vs later failure label."""
    y = sub["is_later_failure"].astype(bool)
    c = sub["early_failure_candidate"].astype(bool)
    n = len(sub)
    tp = int((c & y).sum())
    fp = int((c & ~y).sum())
    fn = int((~c & y).sum())
    prec = tp / (tp + fp) if (tp + fp) else None
    rec = tp / (tp + fn) if (tp + fn) else None
    rate_c = float(y[c].mean()) if c.any() else None
    rate_n = float(y[~c].mean()) if (~c).any() else None
    lift = (rate_c / rate_n) if rate_c is not None and rate_n not in (None, 0) else None
    return {
        "direction": direction,
        "offset_min": offset,
        "n": n,
        "n_candidates": int(c.sum()),
        "n_later_failures": int(y.sum()),
        "precision": prec,
        "recall": rec,
        "failure_rate_candidate": rate_c,
        "failure_rate_non_candidate": rate_n,
        "lift": lift,
        "sample_flag": "OK" if n >= MIN_SAMPLE else "SMALL_SAMPLE",
    }


def decide_primary(pred_rows: list[dict], fwd_rows: list[dict]) -> str:
    """
    DETECTABLE_EARLY_WITH_EDGE: prediction lift on both sides AND forward edge
      (hit60>=0.55 & med60>0) on both sides at some offset<=10.
    DETECTABLE_EARLY_BUT_EDGE_WEAK: early prediction lift exists but forward edge
      is weak, one-sided, or missing.
    ONLY_USEFUL_AFTER_COMPLETION: no robust early prediction lift.
    """
    good_pred = 0
    for d in ("UP", "DOWN"):
        rows = [
            r
            for r in pred_rows
            if r.get("direction") == d
            and r.get("offset_min") in (3, 5, 8, 10)
            and r.get("n_candidates", 0) >= MIN_SAMPLE
            and (r.get("lift") or 0) >= 1.1
            and (r.get("precision") or 0) > (r.get("failure_rate_non_candidate") or 0)
        ]
        if rows:
            good_pred += 1

    good_fwd = 0
    mild_fwd = 0
    for d in ("UP", "DOWN"):
        rows = [
            r
            for r in fwd_rows
            if r.get("direction") == d
            and r.get("slice") == "early_candidate"
            and r.get("offset_min") in (3, 5, 8, 10)
            and r.get("n", 0) >= MIN_SAMPLE
        ]
        if not rows:
            continue
        best = max(rows, key=lambda r: (r.get("median_dir_ret_60m") or -999))
        if (best.get("hit_rate_60m") or 0) >= 0.55 and (best.get("median_dir_ret_60m") or 0) > 0:
            good_fwd += 1
        elif (best.get("hit_rate_60m") or 0) > 0.50 or (best.get("median_dir_ret_60m") or 0) > 0:
            mild_fwd += 1

    if good_pred == 2 and good_fwd == 2:
        return "15M_FAILURE_DETECTABLE_EARLY_WITH_EDGE"
    if good_pred >= 1:
        return "15M_FAILURE_DETECTABLE_EARLY_BUT_EDGE_WEAK"
    return "15M_FAILURE_ONLY_USEFUL_AFTER_COMPLETION"


def decide_partial_eff(pred_rows: list[dict], decay_rows: list[dict]) -> str:
    """Partial efficiency / candidate usefulness as early warning."""
    useful = 0
    for d in ("UP", "DOWN"):
        rows = [
            r
            for r in pred_rows
            if r.get("direction") == d
            and r.get("offset_min") in (5, 8, 10)
            and r.get("n_candidates", 0) >= MIN_SAMPLE
        ]
        if any((r.get("lift") or 0) >= 1.1 for r in rows):
            useful += 1
    # decay overlay descriptive bonus not required
    if useful == 2:
        return "PARTIAL_EFFICIENCY_IS_USEFUL_EARLY_WARNING"
    if useful == 1:
        return "PARTIAL_EFFICIENCY_IS_USEFUL_EARLY_WARNING"
    return "PARTIAL_EFFICIENCY_NOT_ROBUST"


def run_analysis() -> dict[str, Any]:
    print("[load] waves + labels", flush=True)
    waves = load_waves_15m()
    labels = load_failure_labels()
    print(f"[load] waves={len(waves)} failures={len(labels)}", flush=True)

    print("[load] candles 1m/15m", flush=True)
    c1 = prepare_ohlcv("1m")
    c15 = prepare_ohlcv("15m")
    waves_1m = load_micro_waves("1m")
    waves_5m = load_micro_waves("5m")

    print("[build] intra-wave snapshots", flush=True)
    snap, persist = build_snapshots(
        waves=waves,
        labels=labels,
        c1=c1,
        c15=c15,
        waves_1m=waves_1m,
        waves_5m=waves_5m,
    )
    print(f"[build] snapshots={len(snap)}", flush=True)

    # --- Test 1 prediction ---
    pred_rows = []
    for direction in ("UP", "DOWN"):
        for off in SNAPSHOT_OFFSETS_MIN:
            if off == 15:
                continue  # completion not early
            sub = snap[(snap["direction"] == direction) & (snap["offset_min"] == off)]
            pred_rows.append(prediction_stats(sub, direction=direction, offset=int(off)))

    # --- Test 2 forward from early candidates ---
    print("[fwd] attach outcomes", flush=True)
    c5 = load_5m(symbol=SYMBOL)
    # restrict to early offsets for trading signal evaluation
    early = snap[snap["offset_min"] < 15].copy()
    early = attach_forward(early, c5)

    fwd_rows = []
    for direction in ("UP", "DOWN"):
        for off in (3, 5, 8, 10, 12):
            base = early[(early["direction"] == direction) & (early["offset_min"] == off)]
            cand = base[base["early_failure_candidate"]]
            all_dir = base
            later_fail_no_cand = base[base["is_later_failure"] & ~base["early_failure_candidate"]]
            # completion baseline: same waves at offset 15 if present
            comp = snap[(snap["direction"] == direction) & (snap["offset_min"] == 15)]
            # attach forward for completion separately
            fwd_rows.append(metrics(cand, direction=direction, offset_min=off, slice="early_candidate"))
            fwd_rows.append(metrics(all_dir, direction=direction, offset_min=off, slice="all_same_direction"))
            fwd_rows.append(
                metrics(
                    later_fail_no_cand,
                    direction=direction,
                    offset_min=off,
                    slice="later_fail_no_early_cand",
                )
            )

    # completion signal forward (wave end) for comparison
    comp_all = snap[snap["offset_min"] == 15].copy()
    comp_fail = comp_all[comp_all["is_later_failure"]].copy()
    if not comp_fail.empty:
        comp_fail = attach_forward(comp_fail, c5)
        for direction in ("UP", "DOWN"):
            sub = comp_fail[comp_fail["direction"] == direction]
            fwd_rows.append(
                metrics(sub, direction=direction, offset_min=15, slice="completion_failure")
            )

    # --- Lead time ---
    lead_rows = []
    fails = snap[snap["is_later_failure"] & (snap["offset_min"] < 15)]
    first = (
        fails[fails["early_failure_candidate"]]
        .sort_values(["wave_i", "offset_min"])
        .groupby("wave_i", sort=False)
        .head(1)
    )
    if not first.empty:
        first = first.copy()
        first["lead_min"] = first["minutes_to_wave_end"]
        first = attach_forward(first, c5)
        for name, lo, hi in LEAD_BUCKETS:
            sub = first[(first["lead_min"] >= lo) & (first["lead_min"] < hi)]
            for direction in ("UP", "DOWN"):
                s2 = sub[sub["direction"] == direction]
                m = metrics(s2, direction=direction, bucket=name)
                m["median_lead_min"] = float(s2["lead_min"].median()) if len(s2) else None
                lead_rows.append(m)
        # overall
        n_fail_by_dir = (
            labels.assign(
                direction=labels["failure_type"].map(
                    {"FAILED_UP_WAVE": "UP", "FAILED_DOWN_WAVE": "DOWN"}
                )
            )
            .groupby("direction")["wave_i"]
            .nunique()
            .to_dict()
        )
        for direction in ("UP", "DOWN"):
            s2 = first[first["direction"] == direction]
            m = metrics(s2, direction=direction, bucket="ALL_FIRST_CANDIDATE")
            m["median_lead_min"] = float(s2["lead_min"].median()) if len(s2) else None
            m["n_with_any_candidate"] = int(len(s2))
            m["n_failures"] = int(n_fail_by_dir.get(direction, 0))
            m["candidate_coverage"] = (
                (m["n_with_any_candidate"] / m["n_failures"]) if m["n_failures"] else None
            )
            lead_rows.append(m)

    # --- Persistence ---
    persist_out = []
    for direction in ("UP", "DOWN"):
        sub0 = persist[persist["direction"] == direction]
        for name, lo, hi in PERSIST_BUCKETS:
            sub = sub0[
                (sub0["max_partial_fail_streak_1m"] >= lo)
                & (sub0["max_partial_fail_streak_1m"] <= hi)
            ]
            n = len(sub)
            fr = float(sub["is_later_failure"].mean()) if n else None
            persist_out.append(
                {
                    "direction": direction,
                    "bucket": name,
                    "n": n,
                    "later_failure_rate": fr,
                    "sample_flag": "OK" if n >= MIN_SAMPLE else "SMALL_SAMPLE",
                }
            )

    # attach persist outcomes for confirmed failures in streak buckets via completion fwd
    # (descriptive failure rate is enough; optional dir edge on completion)
    # Enrich with completion forward for failures in each bucket
    if not comp_fail.empty:
        streak_map = persist.set_index("wave_i")["max_partial_fail_streak_1m"]
        tmp = comp_fail.copy()
        tmp["streak"] = tmp["wave_i"].map(streak_map)
        for direction in ("UP", "DOWN"):
            for name, lo, hi in PERSIST_BUCKETS:
                sub = tmp[
                    (tmp["direction"] == direction)
                    & (tmp["streak"] >= lo)
                    & (tmp["streak"] <= hi)
                ]
                persist_out.append(
                    metrics(sub, direction=direction, bucket=f"{name}_completion_fwd")
                )

    # --- Efficiency decay ---
    decay_rows = []
    for direction in ("UP", "DOWN"):
        for off in (5, 8, 10):
            base = early[(early["direction"] == direction) & (early["offset_min"] == off)]
            for label in ("EFFICIENCY_DECAYING", "EFFICIENCY_STABLE", "EFFICIENCY_IMPROVING", "NA"):
                sub = base[base["efficiency_path"] == label]
                # prediction vs failure
                y = sub["is_later_failure"].astype(bool)
                decay_rows.append(
                    {
                        "direction": direction,
                        "offset_min": off,
                        "efficiency_path": label,
                        "n": int(len(sub)),
                        "later_failure_rate": float(y.mean()) if len(sub) else None,
                        **{
                            k: v
                            for k, v in metrics(sub, direction=direction).items()
                            if k.startswith("hit_") or k.startswith("median_")
                        },
                    }
                )
            # compare decaying vs partial_eff<=0 candidate
            pe = base[base["partial_directional_efficiency"].astype(float) <= 0]
            dec = base[base["efficiency_path"] == "EFFICIENCY_DECAYING"]
            decay_rows.append(
                {
                    "direction": direction,
                    "offset_min": off,
                    "efficiency_path": "COMPARE_decay_vs_partial_eff_le0",
                    "n_decay": int(len(dec)),
                    "n_partial_le0": int(len(pe)),
                    "fail_rate_decay": float(dec["is_later_failure"].mean()) if len(dec) else None,
                    "fail_rate_partial_le0": float(pe["is_later_failure"].mean()) if len(pe) else None,
                    "hit60_decay": metrics(dec).get("hit_rate_60m"),
                    "hit60_partial_le0": metrics(pe).get("hit_rate_60m"),
                    "med60_decay": metrics(dec).get("median_dir_ret_60m"),
                    "med60_partial_le0": metrics(pe).get("median_dir_ret_60m"),
                }
            )

    # --- Micro overlay on candidates ---
    micro_rows = []
    for direction in ("UP", "DOWN"):
        for off in (5, 8, 10):
            base = early[
                (early["direction"] == direction)
                & (early["offset_min"] == off)
                & (early["early_failure_candidate"])
            ]
            variants = {
                "base": base,
                "plus_m1_counter": base[base["overlay_m1_counter"]],
                "plus_m5_counter": base[base["overlay_m5_counter"]],
                "plus_m1_and_m5_counter": base[
                    base["overlay_m1_counter"] & base["overlay_m5_counter"]
                ],
            }
            for name, sub in variants.items():
                y = sub["is_later_failure"].astype(bool)
                m = metrics(sub, direction=direction, offset_min=off, slice=name)
                m["precision_later_failure"] = float(y.mean()) if len(sub) else None
                m["recall_note"] = "recall vs all failures at offset not recomputed here"
                micro_rows.append(m)

    primary = decide_primary(pred_rows, fwd_rows)
    partial = decide_partial_eff(pred_rows, decay_rows)

    # compact snapshot export (may be large)
    keep = [
        c
        for c in snap.columns
        if c
        in {
            "wave_i",
            "direction",
            "offset_min",
            "snapshot_time",
            "wave_start_available_at",
            "wave_end_available_at",
            "is_later_failure",
            "failure_type",
            "expected_reversal",
            "partial_price_move_pct",
            "partial_signed_price_move_pct",
            "partial_fav_pct",
            "partial_adv_pct",
            "partial_stoch_move",
            "partial_directional_efficiency",
            "stoch_k",
            "stoch_d",
            "stoch_kd",
            "stoch_with_wave",
            "early_failure_candidate",
            "rsi",
            "rsi_delta",
            "rsi_falling",
            "price_vs_ema20",
            "ema9_vs_ema20",
            "efficiency_path",
            "overlay_m1_counter",
            "overlay_m5_counter",
            "minutes_to_wave_end",
            "max_partial_fail_streak_1m",
            "m1_direction",
            "m5_direction",
        }
    ]

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "n_waves": int(len(waves)),
        "n_failures": int(len(labels)),
        "n_snapshots": int(len(snap)),
        "intra_wave_snapshots": snap[keep],
        "early_failure_prediction": pred_rows,
        "early_failure_forward_returns": fwd_rows,
        "lead_time_results": lead_rows,
        "failure_persistence": persist_out,
        "efficiency_decay_results": decay_rows,
        "micro_tf_overlay": micro_rows,
        "persistence_raw": persist,
        "decisions": {"primary": primary, "partial_efficiency": partial},
        "method": METHOD_DOC,
    }
