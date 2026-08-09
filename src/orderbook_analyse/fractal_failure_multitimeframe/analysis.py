"""Orchestrate multi-TF wave-failure analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_failure_multitimeframe import (
    ALL_TFS,
    AUDIT_VERSION,
    EDGE_DELAYS_BY_TF,
    FAILURE_DOC,
    HORIZONS_BY_TF,
    MAIN_HORIZON_BY_TF,
    METHOD_DOC,
    MIN_SAMPLE,
    ROUNDTRIP_FEE_PCT,
    SYMBOL,
    TRADING_TFS,
)
from orderbook_analyse.fractal_failure_multitimeframe.events import (
    annotate_waves,
    failure_events,
)
from orderbook_analyse.fractal_failure_multitimeframe.outcomes import (
    attach_forward_with_opens,
    first_touch_counts,
    load_1m,
    summarize_returns,
    touch_levels_for_tf,
)


def _side_slices(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    return [
        ("COMBINED", df),
        ("LONG", df[df["side"] == "LONG"]),
        ("SHORT", df[df["side"] == "SHORT"]),
    ]


def decide_tf(main_rows: list[dict], baseline_rows: list[dict], tf: str) -> str:
    """
    HAS_EDGE: both failure types at main horizon hit>=0.52, median>0,
              median_net_after_fee>0, and hit lift vs NON_FAILED same dir >=0.03
              (or vs ALL >=0.03) with n>=MIN_SAMPLE.
    CONTEXT: only one type / mixed / lift only on one side.
    NO_EDGE: otherwise.
    """
    h = MAIN_HORIZON_BY_TF[tf]
    by_fail = {
        (r.get("failure_type"), r.get("side")): r
        for r in main_rows
        if r.get("slice") == "FAILURE"
    }
    good = mild = 0
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
        r = by_fail.get((ftype, "COMBINED"))
        if not r or r.get("n", 0) < MIN_SAMPLE:
            continue
        hit = r.get(f"hit_rate_{h}m") or 0
        med = r.get(f"median_dir_ret_{h}m") or 0
        net = r.get(f"median_net_after_fee_{h}m") or 0
        # baseline NON_FAILED same direction
        direction = "UP" if ftype == "FAILED_UP_WAVE" else "DOWN"
        base = next(
            (
                b
                for b in baseline_rows
                if b.get("baseline") == "NON_FAILED_SAME_DIR"
                and b.get("direction") == direction
                and b.get("side") == "COMBINED"
            ),
            None,
        )
        all_b = next(
            (
                b
                for b in baseline_rows
                if b.get("baseline") == "ALL_WAVES" and b.get("side") == "COMBINED"
            ),
            None,
        )
        lift = None
        if base and base.get(f"hit_rate_{h}m") is not None:
            lift = hit - (base.get(f"hit_rate_{h}m") or 0)
        elif all_b and all_b.get(f"hit_rate_{h}m") is not None:
            lift = hit - (all_b.get(f"hit_rate_{h}m") or 0)
        if hit >= 0.52 and med > 0 and net > 0 and lift is not None and lift >= 0.03:
            good += 1
        elif hit > 0.50 and med > 0:
            mild += 1
        elif med > 0 or (lift is not None and lift > 0):
            mild += 1
    if good == 2:
        return "FAILURE_SIGNAL_HAS_EDGE"
    if good == 1 or mild >= 1:
        return "FAILURE_SIGNAL_CONTEXT_DEPENDENT"
    return "FAILURE_SIGNAL_NO_EDGE"


def decide_overall(tf_decisions: dict[str, str], ranking_rows: list[dict]) -> str:
    """
    GENERALIZES: >=3 trading TFs show absolute failure-fade edge on main horizon
                 (hit>=0.52 & median>0), regardless of incremental lift.
    ONLY_15M: only 15m shows that absolute edge among trading TFs.
    SPECIFIC: mixed / sparse.
    """
    abs_edge_tfs = []
    for r in ranking_rows:
        if r.get("failure_type") != "ALL":
            continue
        tf = r.get("timeframe")
        if tf not in TRADING_TFS:
            continue
        if (
            (r.get("n") or 0) >= MIN_SAMPLE
            and (r.get("hit_rate") or 0) >= 0.52
            and (r.get("median_dir_ret") or 0) > 0
        ):
            abs_edge_tfs.append(tf)

    has = [tf for tf, d in tf_decisions.items() if d == "FAILURE_SIGNAL_HAS_EDGE" and tf in TRADING_TFS]
    if len(has) >= 3 or len(abs_edge_tfs) >= 3:
        return "WAVE_FAILURE_GENERALIZES_ACROSS_TIMEFRAMES"
    if abs_edge_tfs == ["15m"] or (has == ["15m"] and len(abs_edge_tfs) <= 1):
        return "WAVE_FAILURE_ONLY_WORKS_ON_15M"
    if len(has) == 1 and has[0] == "15m" and all(
        tf_decisions.get(tf) == "FAILURE_SIGNAL_NO_EDGE" for tf in TRADING_TFS if tf != "15m"
    ):
        return "WAVE_FAILURE_ONLY_WORKS_ON_15M"
    return "WAVE_FAILURE_EDGE_IS_TIMEFRAME_SPECIFIC"


def monthly_stability(df: pd.DataFrame, tf: str, horizon: int) -> list[dict]:
    rows = []
    col = f"dir_ret_{horizon}m"
    if col not in df.columns or df.empty:
        return rows
    tmp = df.copy()
    tmp["month"] = pd.to_datetime(tmp["entry_time"], utc=True).dt.strftime("%Y-%m")
    for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE", "ALL_FAILURES"):
        sub0 = tmp if ftype == "ALL_FAILURES" else tmp[tmp["failure_type"] == ftype]
        for month, sub in sub0.groupby("month"):
            r = sub[col].astype(float).dropna()
            n = int(len(r))
            rows.append(
                {
                    "timeframe": tf,
                    "failure_type": ftype,
                    "month": month,
                    "n": n,
                    "horizon_min": horizon,
                    "hit_rate": float((r > 0).mean()) if n else None,
                    "median_dir_ret": float(r.median()) if n else None,
                    "sample_flag": (
                        "VERY_SMALL_SAMPLE"
                        if n < 10
                        else ("SMALL_SAMPLE" if n < MIN_SAMPLE else "OK")
                    ),
                }
            )
    return rows


def run_analysis() -> dict[str, Any]:
    print("[load] 1m candles", flush=True)
    c1 = load_1m()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    opens = c1["open"].astype(float).to_numpy()
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

    all_fail_events = []
    forward_rows = []
    baseline_rows = []
    first_touch_rows = []
    decay_rows = []
    strength_rows = []
    asymmetry_rows = []
    monthly_rows = []
    ranking_rows = []
    tf_decisions: dict[str, str] = {}
    per_tf_main: dict[str, list[dict]] = {}
    per_tf_base: dict[str, list[dict]] = {}

    for tf in ALL_TFS:
        print(f"\n===== TF {tf} =====", flush=True)
        horizons = HORIZONS_BY_TF[tf]
        main_h = MAIN_HORIZON_BY_TF[tf]
        waves = annotate_waves(tf)
        fails = failure_events(waves)
        print(f"[waves] n={len(waves)} failures={len(fails)}", flush=True)

        # 1m: diagnostic counts only (too many waves for full path baseline)
        if tf == "1m":
            for side_name, sub in _side_slices(fails):
                forward_rows.append(
                    {
                        "timeframe": tf,
                        "slice": "FAILURE",
                        "failure_type": "ALL",
                        "side": side_name,
                        "n": int(len(sub)),
                        "sample_flag": "DIAGNOSTIC_ONLY",
                        "note": "1m forward path skipped (diagnostic counts only)",
                    }
                )
            tf_decisions[tf] = "FAILURE_SIGNAL_NO_EDGE"
            continue

        print(f"[path] all waves forward horizons={horizons}", flush=True)
        all_fwd = attach_forward_with_opens(
            waves,
            high=high,
            low=low,
            close=close,
            opens=opens,
            open_times=open_times,
            horizons=horizons,
            delay_min=0,
        )
        # keep only valid entries with main horizon present
        all_fwd = all_fwd[all_fwd["entry_valid"]].copy()
        fail_fwd = all_fwd[all_fwd["is_failed"]].copy()

        # export event-level failure rows (compact)
        keep_cols = [
            "timeframe",
            "symbol",
            "wave_i",
            "direction",
            "failure_type",
            "side",
            "expected_reversal",
            "confirmation_available_at",
            "entry_time",
            "entry_price",
            "entry_i",
            "signed_price_move_pct",
            "directional_efficiency",
            "inefficient_flag",
            "prev_direction",
            "prev_directional_efficiency",
            "prev_more_efficient",
            "eff_gap_prev_minus_cur",
        ] + [f"dir_ret_{h}m" for h in horizons] + [
            f"dir_fav_{h}m" for h in horizons
        ] + [f"dir_adv_{h}m" for h in horizons]
        keep_cols = [c for c in keep_cols if c in fail_fwd.columns]
        all_fail_events.append(fail_fwd[keep_cols])

        # --- failure metrics ---
        tf_main_rows: list[dict] = []
        for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
            sub_f = fail_fwd[fail_fwd["failure_type"] == ftype]
            for side_name, sub in _side_slices(sub_f):
                m = summarize_returns(
                    sub,
                    horizons,
                    timeframe=tf,
                    slice="FAILURE",
                    failure_type=ftype,
                    side=side_name,
                )
                forward_rows.append(m)
                tf_main_rows.append(m)
        for side_name, sub in _side_slices(fail_fwd):
            m = summarize_returns(
                sub,
                horizons,
                timeframe=tf,
                slice="FAILURE",
                failure_type="ALL",
                side=side_name,
            )
            forward_rows.append(m)
            tf_main_rows.append(m)

        # --- baselines ---
        tf_base_rows: list[dict] = []
        for side_name, sub in _side_slices(all_fwd):
            m = summarize_returns(
                sub,
                horizons,
                timeframe=tf,
                baseline="ALL_WAVES",
                direction="ALL",
                side=side_name,
            )
            baseline_rows.append(m)
            tf_base_rows.append(m)
        for direction in ("UP", "DOWN"):
            sub_d = all_fwd[all_fwd["direction"] == direction]
            for side_name, sub in _side_slices(sub_d):
                m = summarize_returns(
                    sub,
                    horizons,
                    timeframe=tf,
                    baseline="ALL_SAME_DIR",
                    direction=direction,
                    side=side_name,
                )
                baseline_rows.append(m)
                tf_base_rows.append(m)
            sub_nf = all_fwd[(all_fwd["direction"] == direction) & (~all_fwd["is_failed"])]
            for side_name, sub in _side_slices(sub_nf):
                m = summarize_returns(
                    sub,
                    horizons,
                    timeframe=tf,
                    baseline="NON_FAILED_SAME_DIR",
                    direction=direction,
                    side=side_name,
                )
                baseline_rows.append(m)
                tf_base_rows.append(m)

        # lifts into forward table for convenience
        for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
            direction = "UP" if ftype == "FAILED_UP_WAVE" else "DOWN"
            fail_m = next(
                r
                for r in tf_main_rows
                if r.get("failure_type") == ftype and r.get("side") == "COMBINED"
            )
            for bname in ("ALL_WAVES", "ALL_SAME_DIR", "NON_FAILED_SAME_DIR"):
                if bname == "ALL_WAVES":
                    base = next(
                        r
                        for r in tf_base_rows
                        if r.get("baseline") == bname and r.get("side") == "COMBINED"
                    )
                else:
                    base = next(
                        r
                        for r in tf_base_rows
                        if r.get("baseline") == bname
                        and r.get("direction") == direction
                        and r.get("side") == "COMBINED"
                    )
                lift = {
                    "timeframe": tf,
                    "failure_type": ftype,
                    "baseline": bname,
                    "n_failure": fail_m.get("n"),
                    "n_baseline": base.get("n"),
                }
                for h in horizons:
                    fh = fail_m.get(f"hit_rate_{h}m")
                    bh = base.get(f"hit_rate_{h}m")
                    fm = fail_m.get(f"median_dir_ret_{h}m")
                    bm = base.get(f"median_dir_ret_{h}m")
                    fe = fail_m.get(f"mean_dir_ret_{h}m")
                    be = base.get(f"mean_dir_ret_{h}m")
                    lift[f"hit_rate_lift_{h}m"] = (
                        None if fh is None or bh is None else fh - bh
                    )
                    lift[f"median_lift_{h}m"] = (
                        None if fm is None or bm is None else fm - bm
                    )
                    lift[f"mean_lift_{h}m"] = (
                        None if fe is None or be is None else fe - be
                    )
                baseline_rows.append(lift)

        per_tf_main[tf] = tf_main_rows
        per_tf_base[tf] = tf_base_rows

        # first touch
        print("[diag] first touch", flush=True)
        levels = touch_levels_for_tf(tf)
        ft = first_touch_counts(fail_fwd, high=high, low=low, levels=levels)
        for r in ft:
            r["timeframe"] = tf
            first_touch_rows.append(r)

        # edge decay
        print("[diag] edge decay", flush=True)
        for delay in EDGE_DELAYS_BY_TF[tf]:
            if delay == 0:
                delayed = fail_fwd
            else:
                delayed = attach_forward_with_opens(
                    fails,
                    high=high,
                    low=low,
                    close=close,
                    opens=opens,
                    open_times=open_times,
                    horizons=(main_h,),
                    delay_min=delay,
                )
                delayed = delayed[delayed["entry_valid"]]
            for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE", "ALL"):
                sub0 = delayed if ftype == "ALL" else delayed[delayed["failure_type"] == ftype]
                for side_name, sub in _side_slices(sub0):
                    decay_rows.append(
                        summarize_returns(
                            sub,
                            (main_h,),
                            timeframe=tf,
                            failure_type=ftype,
                            side=side_name,
                            delay_min=delay,
                            horizon_min=main_h,
                        )
                    )

        # failure strength quantiles
        print("[diag] strength quantiles", flush=True)
        tmp = fail_fwd.copy()
        try:
            tmp["eff_q"] = pd.qcut(
                tmp["directional_efficiency"].astype(float),
                4,
                labels=["Q1", "Q2", "Q3", "Q4"],
                duplicates="drop",
            )
        except ValueError:
            tmp["eff_q"] = "NA"
        for q, sub in tmp.groupby(tmp["eff_q"].astype(str)):
            for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE", "ALL"):
                sub2 = sub if ftype == "ALL" else sub[sub["failure_type"] == ftype]
                strength_rows.append(
                    summarize_returns(
                        sub2,
                        (main_h,),
                        timeframe=tf,
                        failure_type=ftype,
                        eff_quantile=q,
                        horizon_min=main_h,
                        side="COMBINED",
                    )
                )

        # previous-wave asymmetry
        print("[diag] previous-wave asymmetry", flush=True)
        for flag, label in ((True, "PREV_MORE_EFFICIENT"), (False, "PREV_NOT_MORE_EFFICIENT")):
            sub = fail_fwd[fail_fwd["prev_more_efficient"] == flag]
            for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE"):
                sub2 = sub[sub["failure_type"] == ftype]
                asymmetry_rows.append(
                    summarize_returns(
                        sub2,
                        (main_h,),
                        timeframe=tf,
                        failure_type=ftype,
                        asymmetry_group=label,
                        horizon_min=main_h,
                        side="COMBINED",
                    )
                )
        # efficiency gap quartiles (diagnostic)
        gap = fail_fwd["eff_gap_prev_minus_cur"].astype(float)
        try:
            fail_fwd = fail_fwd.copy()
            fail_fwd["gap_q"] = pd.qcut(
                gap, 4, labels=["Q1", "Q2", "Q3", "Q4"], duplicates="drop"
            )
        except ValueError:
            fail_fwd["gap_q"] = "NA"
        for q, sub in fail_fwd.groupby(fail_fwd["gap_q"].astype(str)):
            asymmetry_rows.append(
                summarize_returns(
                    sub,
                    (main_h,),
                    timeframe=tf,
                    failure_type="ALL",
                    asymmetry_group=f"EFF_GAP_{q}",
                    horizon_min=main_h,
                    side="COMBINED",
                )
            )

        # monthly
        print("[diag] monthly", flush=True)
        monthly_rows.extend(monthly_stability(fail_fwd, tf, main_h))

        # decision + ranking
        dec = decide_tf(tf_main_rows, tf_base_rows, tf)
        tf_decisions[tf] = dec
        print(f"[decision] {tf}: {dec}", flush=True)

        mon = [r for r in monthly_rows if r["timeframe"] == tf and r["failure_type"] == "ALL_FAILURES"]
        share_pos = (
            float(np.mean([1 if (r.get("median_dir_ret") or 0) > 0 else 0 for r in mon]))
            if mon
            else None
        )
        for ftype in ("FAILED_UP_WAVE", "FAILED_DOWN_WAVE", "ALL"):
            m = next(
                r
                for r in tf_main_rows
                if r.get("failure_type") == ftype and r.get("side") == "COMBINED"
            )
            direction = (
                "ALL"
                if ftype == "ALL"
                else ("UP" if ftype == "FAILED_UP_WAVE" else "DOWN")
            )
            base = next(
                (
                    r
                    for r in tf_base_rows
                    if r.get("baseline") == "NON_FAILED_SAME_DIR"
                    and r.get("direction") == (direction if direction != "ALL" else "UP")
                    and r.get("side") == "COMBINED"
                ),
                None,
            )
            if ftype == "ALL":
                # use ALL_WAVES baseline for ALL failures
                base = next(
                    r
                    for r in tf_base_rows
                    if r.get("baseline") == "ALL_WAVES" and r.get("side") == "COMBINED"
                )
            hit = m.get(f"hit_rate_{main_h}m")
            bhit = base.get(f"hit_rate_{main_h}m") if base else None
            ranking_rows.append(
                {
                    "timeframe": tf,
                    "failure_type": ftype,
                    "n": m.get("n"),
                    "best_preregistered_horizon_min": main_h,
                    "hit_rate": hit,
                    "median_dir_ret": m.get(f"median_dir_ret_{main_h}m"),
                    "mean_dir_ret": m.get(f"mean_dir_ret_{main_h}m"),
                    "median_net_after_fee": m.get(f"median_net_after_fee_{main_h}m"),
                    "hit_rate_lift_vs_baseline": (
                        None if hit is None or bhit is None else hit - bhit
                    ),
                    "baseline_used": base.get("baseline") if base else None,
                    "monthly_share_median_positive": share_pos,
                    "decision": dec,
                    "sample_flag": m.get("sample_flag"),
                }
            )

    overall = decide_overall(tf_decisions, ranking_rows)
    print(f"\n[overall] {overall}", flush=True)

    events_df = (
        pd.concat(all_fail_events, ignore_index=True) if all_fail_events else pd.DataFrame()
    )

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "fee_pct": ROUNDTRIP_FEE_PCT,
        "failure_events_all_tf": events_df,
        "failure_forward_returns": forward_rows,
        "failure_baseline_comparison": baseline_rows,
        "first_touch_by_tf": first_touch_rows,
        "edge_decay_by_tf": decay_rows,
        "failure_strength_by_tf": strength_rows,
        "previous_wave_asymmetry": asymmetry_rows,
        "monthly_stability": monthly_rows,
        "timeframe_ranking": ranking_rows,
        "tf_decisions": tf_decisions,
        "overall_decision": overall,
        "method": {"failure": FAILURE_DOC.strip(), "general": METHOD_DOC.strip()},
        "main_horizons": dict(MAIN_HORIZON_BY_TF),
    }
