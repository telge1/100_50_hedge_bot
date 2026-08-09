"""Orchestrate all-wave Stoch fade analysis."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade import (
    AUDIT_VERSION,
    EDGE_DELAYS_BY_TF,
    HORIZONS_BY_TF,
    MAIN_HORIZON_BY_TF,
    METHOD_DOC,
    MIN_SAMPLE,
    ROUNDTRIP_FEE_PCT,
    SYMBOL,
    TRADING_TFS,
)
from orderbook_analyse.fractal_all_wave_fade.events import load_all_waves
from orderbook_analyse.fractal_failure_multitimeframe.outcomes import (
    attach_forward_with_opens,
    first_touch_counts,
    load_1m,
    summarize_returns,
    touch_levels_for_tf,
)


def _sides(df: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    return [
        ("COMBINED", df),
        ("LONG", df[df["side"] == "LONG"]),
        ("SHORT", df[df["side"] == "SHORT"]),
    ]


def _qcut(series: pd.Series, labels=("Q1", "Q2", "Q3", "Q4")) -> pd.Series:
    try:
        return pd.qcut(series.astype(float), 4, labels=list(labels), duplicates="drop")
    except ValueError:
        return pd.Series("NA", index=series.index, dtype=object)


def decide_tf(ranking_tf: list[dict], comparison_tf: list[dict], tf: str) -> str:
    """
    HAS_EDGE: ALL COMBINED main-H hit>=0.52, median>0, net>0, n>=MIN,
              and BOTH sides (LONG/SHORT) have median>0 & hit>0.50.
    CONTEXT: absolute ALL edge but one side weak / net<=0 / unstable monthly.
    NO_EDGE: otherwise.
    """
    h = MAIN_HORIZON_BY_TF[tf]
    all_c = next(
        (
            r
            for r in ranking_tf
            if r.get("wave_group") == "ALL" and r.get("side") == "COMBINED"
        ),
        None,
    )
    if not all_c or all_c.get("n", 0) < MIN_SAMPLE:
        return "ALL_WAVE_FADE_NO_EDGE"
    hit = all_c.get("hit") or 0
    med = all_c.get("median_return") or 0
    net = all_c.get("net_after_fee") or 0
    sides_ok = 0
    for side in ("LONG", "SHORT"):
        r = next(
            (
                x
                for x in ranking_tf
                if x.get("wave_group") == "ALL" and x.get("side") == side
            ),
            None,
        )
        if r and r.get("n", 0) >= MIN_SAMPLE and (r.get("hit") or 0) > 0.50 and (r.get("median_return") or 0) > 0:
            sides_ok += 1
    monthly_pos = all_c.get("monthly_positive_share")
    if hit >= 0.52 and med > 0 and net > 0 and sides_ok == 2 and (monthly_pos is None or monthly_pos >= 0.55):
        return "ALL_WAVE_FADE_HAS_EDGE"
    if hit > 0.50 and med > 0:
        return "ALL_WAVE_FADE_CONTEXT_DEPENDENT"
    return "ALL_WAVE_FADE_NO_EDGE"


def decide_overall(tf_decisions: dict[str, str]) -> str:
    has = [tf for tf, d in tf_decisions.items() if d == "ALL_WAVE_FADE_HAS_EDGE"]
    ctx = [tf for tf, d in tf_decisions.items() if d == "ALL_WAVE_FADE_CONTEXT_DEPENDENT"]
    if len(has) >= 3:
        return "STOCH_WAVE_END_FADE_IS_GENERAL_SIGNAL"
    if len(has) + len(ctx) >= 3 and len(has) >= 1:
        return "STOCH_WAVE_END_FADE_REQUIRES_CONTEXT"
    if len(ctx) >= 3 and len(has) == 0:
        return "STOCH_WAVE_END_FADE_REQUIRES_CONTEXT"
    if len(has) + len(ctx) >= 1:
        return "STOCH_WAVE_END_FADE_REQUIRES_CONTEXT"
    return "STOCH_WAVE_END_FADE_NOT_ROBUST"


def decide_failure_filter(comparison_rows: list[dict]) -> str:
    """
    Compare FAILED vs NON_FAILED vs ALL on COMBINED main horizon per TF.
    HURTS: FAILED median/hit worse than NON_FAILED on majority of TFs.
    ADDS: FAILED clearly better on majority.
    NOT_NEEDED: ALL/NON_FAILED already good; FAILED not better.
    """
    by_tf: dict[str, dict[str, dict]] = {}
    for r in comparison_rows:
        if r.get("side") != "COMBINED":
            continue
        tf = r.get("timeframe")
        g = r.get("wave_group")
        if tf and g:
            by_tf.setdefault(tf, {})[g] = r

    adds = hurts = equalish = 0
    for tf, gmap in by_tf.items():
        if tf not in TRADING_TFS:
            continue
        h = MAIN_HORIZON_BY_TF[tf]
        f = gmap.get("FAILED")
        nf = gmap.get("NON_FAILED")
        if not f or not nf:
            continue
        f_med = f.get(f"median_dir_ret_{h}m")
        nf_med = nf.get(f"median_dir_ret_{h}m")
        f_hit = f.get(f"hit_rate_{h}m")
        nf_hit = nf.get(f"hit_rate_{h}m")
        if f_med is None or nf_med is None:
            continue
        better = (f_med > nf_med + 1e-9) and (f_hit is not None and nf_hit is not None and f_hit >= nf_hit)
        worse = (f_med < nf_med - 1e-9) and (f_hit is not None and nf_hit is not None and f_hit <= nf_hit)
        if better:
            adds += 1
        elif worse:
            hurts += 1
        else:
            equalish += 1
    if hurts >= max(adds, 1) and hurts >= 2:
        return "FAILURE_FILTER_HURTS_EDGE"
    if adds >= 3 and adds > hurts:
        return "FAILURE_FILTER_ADDS_VALUE"
    return "FAILURE_FILTER_NOT_NEEDED"


def monthly_rows(df: pd.DataFrame, tf: str, horizon: int) -> list[dict]:
    col = f"dir_ret_{horizon}m"
    if col not in df.columns or df.empty:
        return []
    tmp = df.copy()
    tmp["month"] = pd.to_datetime(tmp["entry_time"], utc=True).dt.strftime("%Y-%m")
    out = []
    for side_name, sub0 in _sides(tmp):
        for month, sub in sub0.groupby("month"):
            r = sub[col].astype(float).dropna()
            n = int(len(r))
            med = float(r.median()) if n else None
            out.append(
                {
                    "timeframe": tf,
                    "side": side_name,
                    "month": month,
                    "n": n,
                    "horizon_min": horizon,
                    "hit_rate": float((r > 0).mean()) if n else None,
                    "median_dir_ret": med,
                    "median_net_after_fee": None if med is None else med - ROUNDTRIP_FEE_PCT,
                    "sample_flag": (
                        "VERY_SMALL_SAMPLE"
                        if n < 10
                        else ("SMALL_SAMPLE" if n < MIN_SAMPLE else "OK")
                    ),
                }
            )
    return out


def run_analysis() -> dict[str, Any]:
    print("[load] 1m candles", flush=True)
    c1 = load_1m()
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    opens = c1["open"].astype(float).to_numpy()
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

    event_frames = []
    forward_rows: list[dict] = []
    comparison_rows: list[dict] = []
    zone_rows: list[dict] = []
    path_rows: list[dict] = []
    duration_rows: list[dict] = []
    eff_rows: list[dict] = []
    size_rows: list[dict] = []
    rsi_rows: list[dict] = []
    ema_rows: list[dict] = []
    prev_rows: list[dict] = []
    decay_rows: list[dict] = []
    touch_rows: list[dict] = []
    month_rows: list[dict] = []
    ranking_rows: list[dict] = []
    tf_decisions: dict[str, str] = {}

    for tf in TRADING_TFS:
        print(f"\n===== TF {tf} =====", flush=True)
        horizons = HORIZONS_BY_TF[tf]
        main_h = MAIN_HORIZON_BY_TF[tf]
        waves = load_all_waves(tf)
        print(f"[waves] n={len(waves)} failed={int(waves.is_failed.sum())}", flush=True)

        print("[path] forward", flush=True)
        fwd = attach_forward_with_opens(
            waves,
            high=high,
            low=low,
            close=close,
            opens=opens,
            open_times=open_times,
            horizons=horizons,
            delay_min=0,
        )
        fwd = fwd[fwd["entry_valid"]].copy()

        keep = [
            "timeframe",
            "symbol",
            "wave_i",
            "direction",
            "side",
            "expected_reversal",
            "wave_group",
            "failure_type",
            "is_failed",
            "confirmation_available_at",
            "entry_time",
            "entry_price",
            "entry_i",
            "stoch_zone_start",
            "stoch_zone_end",
            "stoch_path",
            "n_bars",
            "duration_bucket",
            "signed_price_move_pct",
            "directional_efficiency",
            "favorable_move_pct",
            "rsi_end",
            "rsi_delta",
            "rsi_bucket",
            "rsi_delta_sign",
            "price_vs_ema20_end",
            "ema9_vs_ema20_end",
            "ema_context",
            "prev_direction",
            "prev_directional_efficiency",
            "prev_signed_price_move_pct",
            "prev_n_bars",
            "prev_duration_min",
            "prev_rel_efficiency",
        ]
        keep += [f"dir_ret_{h}m" for h in horizons]
        keep += [f"dir_fav_{h}m" for h in horizons]
        keep += [f"dir_adv_{h}m" for h in horizons]
        keep = [c for c in keep if c in fwd.columns]
        event_frames.append(fwd[keep])

        # --- main groups: ALL / FAILED / NON_FAILED x sides ---
        tf_rank: list[dict] = []
        for group_name, mask in (
            ("ALL", pd.Series(True, index=fwd.index)),
            ("FAILED", fwd["is_failed"]),
            ("NON_FAILED", ~fwd["is_failed"]),
        ):
            sub_g = fwd[mask]
            for side_name, sub in _sides(sub_g):
                m = summarize_returns(
                    sub,
                    horizons,
                    timeframe=tf,
                    wave_group=group_name,
                    side=side_name,
                )
                forward_rows.append(m)
                comparison_rows.append(m)
                # ranking row on main horizon
                med = m.get(f"median_dir_ret_{main_h}m")
                hit = m.get(f"hit_rate_{main_h}m")
                tf_rank.append(
                    {
                        "timeframe": tf,
                        "side": side_name,
                        "wave_group": group_name,
                        "n": m.get("n"),
                        "main_horizon": main_h,
                        "hit": hit,
                        "median_return": med,
                        "net_after_fee": None if med is None else med - ROUNDTRIP_FEE_PCT,
                        "mean_return": m.get(f"mean_dir_ret_{main_h}m"),
                        "sample_flag": m.get("sample_flag"),
                    }
                )

        # UP vs DOWN explicit
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for side_name, sub in _sides(sub_d):
                forward_rows.append(
                    summarize_returns(
                        sub,
                        horizons,
                        timeframe=tf,
                        wave_group="ALL",
                        direction=direction,
                        side=side_name,
                        slice="BY_DIRECTION",
                    )
                )

        # Stoch end zone
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for zone, sub in sub_d.groupby(sub_d["stoch_zone_end"].astype(str)):
                zone_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        stoch_zone_end=zone,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # Stoch path start->end
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for path, sub in sub_d.groupby(sub_d["stoch_path"].astype(str)):
                path_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        stoch_path=path,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # Duration
        for direction in ("UP", "DOWN", "ALL"):
            sub0 = fwd if direction == "ALL" else fwd[fwd["direction"] == direction]
            for bucket, sub in sub0.groupby(sub0["duration_bucket"].astype(str)):
                duration_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        duration_bucket=bucket,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # Efficiency quantiles by direction
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction].copy()
            sub_d["eff_q"] = _qcut(sub_d["directional_efficiency"])
            for q, sub in sub_d.groupby(sub_d["eff_q"].astype(str)):
                eff_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        eff_quantile=q,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # Wave size quantiles (signed_price_move_pct)
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction].copy()
            sub_d["size_q"] = _qcut(sub_d["signed_price_move_pct"])
            for q, sub in sub_d.groupby(sub_d["size_q"].astype(str)):
                size_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        size_quantile=q,
                        metric="signed_price_move_pct",
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )
            if "favorable_move_pct" in sub_d.columns:
                sub_d["fav_q"] = _qcut(sub_d["favorable_move_pct"])
                for q, sub in sub_d.groupby(sub_d["fav_q"].astype(str)):
                    size_rows.append(
                        summarize_returns(
                            sub,
                            (main_h,),
                            timeframe=tf,
                            direction=direction,
                            size_quantile=q,
                            metric="favorable_move_pct",
                            side="COMBINED",
                            horizon_min=main_h,
                        )
                    )

        # RSI context
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for bucket, sub in sub_d.groupby(sub_d["rsi_bucket"].astype(str)):
                rsi_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        rsi_bucket=bucket,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )
            for sgn, sub in sub_d.groupby(sub_d["rsi_delta_sign"].astype(str)):
                rsi_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        rsi_delta_sign=sgn,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # EMA context
        for direction in ("UP", "DOWN"):
            sub_d = fwd[fwd["direction"] == direction]
            for ctx, sub in sub_d.groupby(sub_d["ema_context"].astype(str)):
                ema_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        ema_context=ctx,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # Previous wave relation
        for direction in ("UP", "DOWN", "ALL"):
            sub0 = fwd if direction == "ALL" else fwd[fwd["direction"] == direction]
            for rel, sub in sub0.groupby(sub0["prev_rel_efficiency"].astype(str)):
                prev_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        direction=direction,
                        prev_rel_efficiency=rel,
                        side="COMBINED",
                        horizon_min=main_h,
                    )
                )

        # Edge decay
        print("[diag] edge decay", flush=True)
        for delay in EDGE_DELAYS_BY_TF[tf]:
            if delay == 0:
                delayed = fwd
            else:
                delayed = attach_forward_with_opens(
                    waves,
                    high=high,
                    low=low,
                    close=close,
                    opens=opens,
                    open_times=open_times,
                    horizons=(main_h,),
                    delay_min=delay,
                )
                delayed = delayed[delayed["entry_valid"]]
            for side_name, sub in _sides(delayed):
                decay_rows.append(
                    summarize_returns(
                        sub,
                        (main_h,),
                        timeframe=tf,
                        wave_group="ALL",
                        side=side_name,
                        delay_min=delay,
                        horizon_min=main_h,
                    )
                )

        # First touch
        print("[diag] first touch", flush=True)
        levels = touch_levels_for_tf(tf)
        for r in first_touch_counts(fwd, high=high, low=low, levels=levels):
            r["timeframe"] = tf
            touch_rows.append(r)

        # Monthly
        print("[diag] monthly", flush=True)
        mrows = monthly_rows(fwd, tf, main_h)
        month_rows.extend(mrows)
        mon_c = [r for r in mrows if r["side"] == "COMBINED"]
        share_pos = (
            float(np.mean([1 if (r.get("median_dir_ret") or 0) > 0 else 0 for r in mon_c]))
            if mon_c
            else None
        )
        for row in tf_rank:
            row["monthly_positive_share"] = share_pos if row["side"] == "COMBINED" else None
            # side-specific monthly share
            if row["side"] in ("LONG", "SHORT"):
                mon_s = [r for r in mrows if r["side"] == row["side"]]
                row["monthly_positive_share"] = (
                    float(
                        np.mean(
                            [1 if (r.get("median_dir_ret") or 0) > 0 else 0 for r in mon_s]
                        )
                    )
                    if mon_s
                    else None
                )
            ranking_rows.append(row)

        dec = decide_tf(tf_rank, comparison_rows, tf)
        tf_decisions[tf] = dec
        print(f"[decision] {tf}: {dec}", flush=True)

    overall = decide_overall(tf_decisions)
    fail_dec = decide_failure_filter(comparison_rows)
    print(f"[overall] {overall}", flush=True)
    print(f"[failure_filter] {fail_dec}", flush=True)

    events_df = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()

    return {
        "audit_version": AUDIT_VERSION,
        "symbol": SYMBOL,
        "fee_pct": ROUNDTRIP_FEE_PCT,
        "all_wave_events": events_df,
        "all_wave_forward_returns": forward_rows,
        "failure_vs_all_comparison": comparison_rows,
        "stoch_end_zone_results": zone_rows,
        "stoch_path_results": path_rows,
        "wave_duration_results": duration_rows,
        "efficiency_quantiles": eff_rows,
        "wave_size_quantiles": size_rows,
        "rsi_context_results": rsi_rows,
        "ema_context_results": ema_rows,
        "previous_wave_results": prev_rows,
        "edge_decay": decay_rows,
        "first_touch": touch_rows,
        "monthly_stability": month_rows,
        "timeframe_ranking": ranking_rows,
        "tf_decisions": tf_decisions,
        "overall_decision": overall,
        "failure_filter_decision": fail_dec,
        "method": METHOD_DOC.strip(),
        "main_horizons": dict(MAIN_HORIZON_BY_TF),
    }
