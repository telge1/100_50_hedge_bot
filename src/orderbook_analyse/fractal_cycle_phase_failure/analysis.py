"""Orchestrate cycle-phase × 15m-failure analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_phase_failure import (
    AUDIT_VERSION,
    FAILURE_DOC,
    MIN_SAMPLE,
    PHASE_DOC,
    SYMBOL,
)
from orderbook_analyse.fractal_cycle_phase_failure.events import (
    attach_context,
    attach_relative_weakness,
    build_failure_events,
    micro_diagnostic,
)
from orderbook_analyse.fractal_cycle_phase_failure.outcomes import (
    attach_outcomes,
    load_5m_path,
    metrics,
)
from orderbook_analyse.fractal_cycle_phase_failure.phase import (
    early_late_bucket,
    rsi_bucket,
)


def decide_cycle_phase(early_late_rows: list[dict], phase_1d_rows: list[dict]) -> str:
    """
    CONDITIONS_FAILURE_DIRECTION: late vs early shows >=5pp hit lift and better med60
      on BOTH failure types (with n>=30 each side).
    EFFECT_WEAK: lift on one side or only milder.
    NO_VALUE: otherwise.
    """
    lifts = []
    for ftype, late_key, early_key in (
        ("FAILED_UP_WAVE", "LATE_UP", "EARLY_UP"),
        ("FAILED_DOWN_WAVE", "LATE_DOWN", "EARLY_DOWN"),
    ):
        # prefer 1D early/late
        late = [
            r
            for r in early_late_rows
            if r.get("failure_type") == ftype
            and r.get("tf") == "1d"
            and r.get("bucket") == late_key
        ]
        early = [
            r
            for r in early_late_rows
            if r.get("failure_type") == ftype
            and r.get("tf") == "1d"
            and r.get("bucket") == early_key
        ]
        if not late or not early:
            continue
        L, E = late[0], early[0]
        if L.get("n", 0) < MIN_SAMPLE or E.get("n", 0) < MIN_SAMPLE:
            continue
        hit_lift = (L.get("hit_rate_60m") or 0) - (E.get("hit_rate_60m") or 0)
        med_lift = (L.get("median_dir_ret_60m") or 0) - (E.get("median_dir_ret_60m") or 0)
        lifts.append({"hit_lift": hit_lift, "med_lift": med_lift, "ftype": ftype})

    if not lifts:
        # fallback: dispersion across 1D phases
        edges = 0
        for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
            rows = [
                r
                for r in phase_1d_rows
                if r.get("failure_type") == ftype and r.get("n", 0) >= MIN_SAMPLE
            ]
            if len(rows) < 2:
                continue
            hits = [r.get("hit_rate_60m") or 0 for r in rows]
            if max(hits) - min(hits) >= 0.05:
                edges += 1
        if edges >= 1:
            return "CYCLE_PHASE_EFFECT_WEAK"
        return "CYCLE_PHASE_NO_VALUE"

    strong = sum(1 for L in lifts if L["hit_lift"] >= 0.05 and L["med_lift"] > 0)
    mild = sum(1 for L in lifts if L["hit_lift"] > 0 or L["med_lift"] > 0)
    if strong == len(lifts):
        return "CYCLE_PHASE_CONDITIONS_FAILURE_DIRECTION"
    if mild >= 1:
        return "CYCLE_PHASE_EFFECT_WEAK"
    return "CYCLE_PHASE_NO_VALUE"


def decide_signal(base_rows: list[dict], conditioned_rows: list[dict]) -> str:
    """
    HAS_EDGE: overall failure hit60>0.52 and med60>0 on both types,
              OR best 1D-phase group clearly above overall with n>=30.
    CONTEXT: only one type / only some phases.
    NO_EDGE: otherwise.
    """
    by = {(r["failure_type"], r.get("slice", "ALL")): r for r in base_rows}
    good = 0
    mild = 0
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        r = by.get((ftype, "ALL"))
        if not r or r.get("n", 0) < MIN_SAMPLE:
            continue
        if (r.get("hit_rate_60m") or 0) >= 0.52 and (r.get("median_dir_ret_60m") or 0) > 0:
            good += 1
        elif (r.get("hit_rate_60m") or 0) > 0.50 or (r.get("median_dir_ret_60m") or 0) > 0:
            mild += 1

    # phase conditioned edge over ALL
    phase_edge = 0
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        base = by.get((ftype, "ALL"))
        if not base:
            continue
        cands = [
            r
            for r in conditioned_rows
            if r.get("failure_type") == ftype
            and r.get("n", 0) >= MIN_SAMPLE
            and (r.get("hit_rate_60m") or 0) - (base.get("hit_rate_60m") or 0) >= 0.05
            and (r.get("median_dir_ret_60m") or 0) > (base.get("median_dir_ret_60m") or 0)
        ]
        if cands:
            phase_edge += 1

    if good == 2 or (good >= 1 and phase_edge == 2):
        return "15M_FAILURE_PHASE_SIGNAL_HAS_EDGE"
    if good >= 1 or mild >= 1 or phase_edge >= 1:
        return "15M_FAILURE_PHASE_SIGNAL_CONTEXT_DEPENDENT"
    return "15M_FAILURE_PHASE_SIGNAL_NO_EDGE"


def run_analysis() -> dict[str, Any]:
    print("[events] 15m failure episodes", flush=True)
    events = build_failure_events()
    print(f"[events] n={len(events)}", flush=True)

    print("[context] MTF join", flush=True)
    df = attach_context(events)
    df = attach_relative_weakness(df)
    df["micro_diag"] = micro_diagnostic(df)
    df["M15_rsi_bucket"] = rsi_bucket(df["M15_rsi_end"])
    df["D1_rsi_bucket"] = rsi_bucket(df["D1_rsi_end"])
    df["D1_early_late_up"] = early_late_bucket(df["D1_cycle_phase"], side_up=True)
    df["D1_early_late_down"] = early_late_bucket(df["D1_cycle_phase"], side_up=False)
    df["H4_early_late_up"] = early_late_bucket(df["H4_cycle_phase"], side_up=True)
    df["H4_early_late_down"] = early_late_bucket(df["H4_cycle_phase"], side_up=False)

    print("[outcomes] 5m forward path", flush=True)
    candles = load_5m_path(symbol=SYMBOL)
    df = attach_outcomes(df, candles)
    # drop if no 1d context
    before = len(df)
    df = df[df["D1_end_available_at"].notna()].copy()
    print(f"[warmup] require 1d: {before} -> {len(df)}", flush=True)

    # --- base summary ---
    base_rows = []
    phase_summary = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub = df[df["failure_type"] == ftype]
        base_rows.append(metrics(sub, failure_type=ftype, slice="ALL"))
        phase_summary.append(metrics(sub, failure_type=ftype, group="ALL"))

    # A) 1D phase
    phase_1d = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        for phase, sub in sub0.groupby(sub0["D1_cycle_phase"].astype(str)):
            phase_1d.append(
                metrics(sub, failure_type=ftype, level="1d", D1_phase=phase)
            )

    # B) 1D + 4h
    phase_1d_4h = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        keys = sub0["D1_cycle_phase"].astype(str) + "|" + sub0["H4_cycle_phase"].astype(str)
        for key, sub in sub0.groupby(keys):
            d1, h4 = str(key).split("|", 1)
            phase_1d_4h.append(
                metrics(sub, failure_type=ftype, level="1d_4h", D1_phase=d1, H4_phase=h4)
            )

    # C) 1D + 4h + 1h
    phase_1d_4h_1h = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        keys = (
            sub0["D1_cycle_phase"].astype(str)
            + "|"
            + sub0["H4_cycle_phase"].astype(str)
            + "|"
            + sub0["H1_cycle_phase"].astype(str)
        )
        for key, sub in sub0.groupby(keys):
            d1, h4, h1 = str(key).split("|", 2)
            phase_1d_4h_1h.append(
                metrics(
                    sub,
                    failure_type=ftype,
                    level="1d_4h_1h",
                    D1_phase=d1,
                    H4_phase=h4,
                    H1_phase=h1,
                )
            )

    # Early vs late
    early_late_rows = []
    for ftype, side_up, late_name, early_name in (
        ("FAILED_UP_WAVE", True, "LATE_UP", "EARLY_UP"),
        ("FAILED_DOWN_WAVE", False, "LATE_DOWN", "EARLY_DOWN"),
    ):
        sub0 = df[df["failure_type"] == ftype]
        for tf, col in (("1d", "D1_cycle_phase"), ("4h", "H4_cycle_phase"), ("1h", "H1_cycle_phase")):
            bucket = early_late_bucket(sub0[col], side_up=side_up)
            for bname in (early_name, late_name, "OTHER"):
                sub = sub0[bucket == bname]
                early_late_rows.append(
                    metrics(
                        sub,
                        failure_type=ftype,
                        tf=tf,
                        bucket=bname,
                    )
                )

    # Relative weakness
    rel_rows = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        for flag, label in ((True, "WITH_RELATIVE_WEAKNESS"), (False, "WITHOUT_RELATIVE_WEAKNESS")):
            sub = sub0[sub0["relative_wave_weakness"] == flag]
            rel_rows.append(metrics(sub, failure_type=ftype, slice=label))

    # RSI context
    rsi_rows = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        for col, label in (("M15_rsi_bucket", "M15_rsi"), ("D1_rsi_bucket", "D1_rsi")):
            for b, sub in sub0.groupby(sub0[col].astype(str)):
                rsi_rows.append(metrics(sub, failure_type=ftype, context=label, bucket=b))
        # RSI delta sign
        delta = sub0["M15_rsi_delta"].astype(float)
        for name, mask in (
            ("M15_rsi_delta_pos", delta > 0),
            ("M15_rsi_delta_neg", delta < 0),
            ("M15_rsi_delta_zero", delta == 0),
        ):
            rsi_rows.append(metrics(sub0[mask], failure_type=ftype, context="M15_rsi_delta", bucket=name))

    # EMA context
    ema_rows = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        for col, label in (
            ("M15_price_vs_ema20_end", "M15_price_vs_ema20"),
            ("M15_ema9_vs_ema20_end", "M15_ema9_vs_ema20"),
            ("D1_price_vs_ema20_end", "D1_price_vs_ema20"),
            ("D1_ema9_vs_ema20_end", "D1_ema9_vs_ema20"),
        ):
            for b, sub in sub0.groupby(sub0[col].astype(str)):
                ema_rows.append(metrics(sub, failure_type=ftype, context=label, bucket=b))

    # Micro diagnostic
    micro_rows = []
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        sub0 = df[df["failure_type"] == ftype]
        for b, sub in sub0.groupby(sub0["micro_diag"].astype(str)):
            micro_rows.append(metrics(sub, failure_type=ftype, micro_diag=b))

    cycle_dec = decide_cycle_phase(early_late_rows, phase_1d)
    signal_dec = decide_signal(base_rows, phase_1d)

    # compact event export columns
    keep = [
        c
        for c in df.columns
        if c
        in {
            "decision_time",
            "symbol",
            "failure_type",
            "expected_reversal",
            "wave_i",
            "M15_direction",
            "M15_cycle_phase",
            "M15_stoch_zone_start",
            "M15_stoch_zone_end",
            "M15_stoch_k_start",
            "M15_stoch_k_end",
            "M15_signed_price_move_pct",
            "M15_directional_efficiency",
            "M15_rsi_end",
            "M15_rsi_delta",
            "M15_price_vs_ema20_end",
            "M15_ema9_vs_ema20_end",
            "D1_cycle_phase",
            "D1_direction",
            "D1_stoch_zone_end",
            "D1_directional_efficiency",
            "D1_rsi_end",
            "D1_rsi_delta",
            "D1_price_vs_ema20_end",
            "D1_ema9_vs_ema20_end",
            "H4_cycle_phase",
            "H4_direction",
            "H4_stoch_zone_end",
            "H4_directional_efficiency",
            "H4_rsi_end",
            "H1_cycle_phase",
            "H1_direction",
            "H1_stoch_zone_end",
            "H1_directional_efficiency",
            "H1_rsi_end",
            "relative_wave_weakness",
            "flag_inefficient_up_in_bear",
            "flag_inefficient_down_in_bull",
            "micro_diag",
            "M5_direction",
            "M5_directional_efficiency",
            "M1m_direction",
            "M1m_directional_efficiency",
            "dir_ret_15m",
            "dir_ret_30m",
            "dir_ret_60m",
            "dir_ret_120m",
            "dir_ret_240m",
            "dir_fav_60m",
            "dir_adv_60m",
            "dir_fav_120m",
            "dir_adv_120m",
        }
        or c.startswith("dir_")
    ]
    # unique preserve order
    seen = set()
    keep_u = []
    for c in keep:
        if c in df.columns and c not in seen:
            keep_u.append(c)
            seen.add(c)

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "n_events": int(len(df)),
        "n_failed_up": int((df["failure_type"] == "FAILED_UP_WAVE").sum()),
        "n_failed_down": int((df["failure_type"] == "FAILED_DOWN_WAVE").sum()),
        "failure_events": df[keep_u],
        "failure_phase_summary": phase_summary,
        "failure_phase_1d": phase_1d,
        "failure_phase_1d_4h": phase_1d_4h,
        "failure_phase_1d_4h_1h": phase_1d_4h_1h,
        "early_late_cycle_results": early_late_rows,
        "relative_wave_weakness": rel_rows,
        "rsi_context_results": rsi_rows,
        "ema_context_results": ema_rows,
        "micro_tf_diagnostic": micro_rows,
        "base_failure_results": base_rows,
        "decisions": {
            "cycle_phase": cycle_dec,
            "signal": signal_dec,
        },
        "method": {
            "phase": PHASE_DOC.strip(),
            "failure": FAILURE_DOC.strip(),
            "causality": "context wave end_available_at <= failure decision_time",
            "no_trend_voting": True,
            "no_threshold_search": True,
            "micro_tf": "diagnostic only",
            "1W_1M": "joined but not used for decisions",
        },
    }
