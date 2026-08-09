"""Classify events and compare ALL vs TREND_ALIGNED vs COUNTERTREND (+ Q4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_all_wave_fade_generalization.annotate import (
    annotate_waves_df,
    load_frozen_quantile_edges,
)
from orderbook_analyse.fractal_failure_multitimeframe.outcomes import (
    attach_forward_with_opens,
    load_1m,
)
from orderbook_analyse.fractal_wave_fade_trend_filter import (
    AUDIT_VERSION,
    FEE_PCT,
    GEN_DIR,
    MAIN_HORIZON_BY_TF,
    MIN_SAMPLE,
    SIGNAL_TFS,
    SOURCE_GENERALIZATION,
    TREND_DOC,
    VERY_SMALL,
    WAVE_CACHE,
)


def sample_flag(n: int) -> str:
    if n < VERY_SMALL:
        return "VERY_SMALL_SAMPLE"
    if n < MIN_SAMPLE:
        return "SMALL_SAMPLE"
    return "OK"


def assign_trend_bucket(df: pd.DataFrame) -> pd.Series:
    """Frozen H4 mapping."""
    out = pd.Series("MIXED", index=df.index, dtype=object)
    up = df["direction"].astype(str) == "UP"
    dn = df["direction"].astype(str) == "DOWN"
    bull = df["ema_context"].astype(str) == "EMA_BULL"
    bear = df["ema_context"].astype(str) == "EMA_BEAR"
    out.loc[up & bull] = "TREND_ALIGNED"
    out.loc[dn & bear] = "TREND_ALIGNED"
    out.loc[up & bear] = "COUNTERTREND"
    out.loc[dn & bull] = "COUNTERTREND"
    # MIXED already default for ema MIXED / other
    return out


def summarize_group(sub: pd.DataFrame, horizon: int, **meta: Any) -> dict[str, Any]:
    col = f"dir_ret_{horizon}m"
    n = int(len(sub))
    row: dict[str, Any] = {**meta, "n": n, "horizon_min": horizon, "sample_flag": sample_flag(n)}
    if n == 0 or col not in sub.columns:
        return row
    r = sub[col].astype(float).dropna()
    nv = int(len(r))
    row["n_valid"] = nv
    if nv == 0:
        return row
    net = r - FEE_PCT
    row["hit_rate"] = float((r > 0).mean())
    row["median_dir_ret"] = float(r.median())
    row["mean_dir_ret"] = float(r.mean())
    row["q25_dir_ret"] = float(r.quantile(0.25))
    row["q75_dir_ret"] = float(r.quantile(0.75))
    row["median_net"] = float(net.median())
    row["mean_net"] = float(net.mean())
    fav_c = f"dir_fav_{horizon}m"
    adv_c = f"dir_adv_{horizon}m"
    if fav_c in sub.columns:
        fv = sub.loc[r.index, fav_c].astype(float)
        row["median_fav"] = float(fv.median()) if fv.notna().any() else None
    if adv_c in sub.columns:
        av = sub.loc[r.index, adv_c].astype(float)
        row["median_adv"] = float(av.median()) if av.notna().any() else None
    # countertrend net class
    med_net = row["median_net"]
    if med_net > 0.02:
        row["net_class"] = "POSITIVE_NET"
    elif med_net < -0.02:
        row["net_class"] = "NEGATIVE_NET"
    else:
        row["net_class"] = "NEAR_ZERO"
    return row


def add_lifts(aligned: dict, baseline: dict, prefix: str) -> dict:
    out = {}
    for key, name in (
        ("hit_rate", "hit_lift"),
        ("median_dir_ret", "median_return_lift"),
        ("median_net", "net_return_lift"),
    ):
        a, b = aligned.get(key), baseline.get(key)
        out[f"{prefix}_{name}"] = None if a is None or b is None else a - b
    return out


def monotonicity_label(nets: list[float | None]) -> str:
    vals = [v for v in nets if v is not None]
    if len(vals) < 4:
        return "INSUFFICIENT"
    ups = sum(1 for i in range(3) if vals[i + 1] > vals[i])
    if ups == 3:
        return "MONOTONIC"
    if ups >= 2:
        return "MOSTLY_MONOTONIC"
    return "NON_MONOTONIC"


def load_cached_waves(symbol: str, tf: str) -> pd.DataFrame:
    path = WAVE_CACHE / symbol / f"waves_{tf}.csv"
    return pd.read_csv(path)


def btc_entry_bounds(c1: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    ts = pd.to_datetime(c1["timestamp"], utc=True)
    return ts.min(), ts.max()


def run_symbol(
    symbol: str,
    *,
    quantile_edges: dict,
    c1: pd.DataFrame,
    entry_start: pd.Timestamp | None = None,
    entry_end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    high = c1["high"].astype(float).to_numpy()
    low = c1["low"].astype(float).to_numpy()
    close = c1["close"].astype(float).to_numpy()
    opens = c1["open"].astype(float).to_numpy()
    open_times = c1["timestamp"].to_numpy(dtype="datetime64[ns]")

    event_frames = []
    comparison = []
    long_short = []
    eff_rows = []
    mono_rows = []
    counter_rows = []
    retention = []
    opportunity = []
    monthly = []

    for tf in SIGNAL_TFS:
        main_h = MAIN_HORIZON_BY_TF[tf]
        raw = load_cached_waves(symbol, tf)
        ann = annotate_waves_df(raw, symbol=symbol, timeframe=tf, quantile_edges=quantile_edges)
        ann["trend_bucket"] = assign_trend_bucket(ann)

        fwd = attach_forward_with_opens(
            ann,
            high=high,
            low=low,
            close=close,
            opens=opens,
            open_times=open_times,
            horizons=(main_h,),
            delay_min=0,
        )
        fwd = fwd[fwd["entry_valid"]].copy()
        if entry_start is not None:
            fwd = fwd[pd.to_datetime(fwd["entry_time"], utc=True) >= entry_start]
        if entry_end is not None:
            fwd = fwd[pd.to_datetime(fwd["entry_time"], utc=True) <= entry_end]

        keep = [
            "symbol",
            "timeframe",
            "wave_i",
            "direction",
            "side",
            "ema_context",
            "trend_bucket",
            "eff_quantile",
            "confirmation_available_at",
            "entry_time",
            "entry_price",
            f"dir_ret_{main_h}m",
            f"dir_fav_{main_h}m",
            f"dir_adv_{main_h}m",
        ]
        keep = [c for c in keep if c in fwd.columns]
        event_frames.append(fwd[keep])

        groups = {
            "ALL": fwd,
            "TREND_ALIGNED": fwd[fwd["trend_bucket"] == "TREND_ALIGNED"],
            "COUNTERTREND": fwd[fwd["trend_bucket"] == "COUNTERTREND"],
            "MIXED": fwd[fwd["trend_bucket"] == "MIXED"],
        }
        summaries = {}
        for gname, sub in groups.items():
            m = summarize_group(
                sub, main_h, symbol=symbol, timeframe=tf, trend_group=gname, side="COMBINED"
            )
            summaries[gname] = m
            comparison.append(m)

        # lifts
        for base_name in ("ALL", "COUNTERTREND"):
            lifts = add_lifts(summaries["TREND_ALIGNED"], summaries[base_name], f"vs_{base_name}")
            comparison.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "side": "COMBINED",
                    "trend_group": f"TREND_ALIGNED_LIFTS_VS_{base_name}",
                    "n": summaries["TREND_ALIGNED"].get("n"),
                    **lifts,
                    "aligned_median_net": summaries["TREND_ALIGNED"].get("median_net"),
                    "base_median_net": summaries[base_name].get("median_net"),
                }
            )

        # long/short explicit aligned setups
        for side, direction, need_ctx in (
            ("LONG", "DOWN", "EMA_BEAR"),
            ("SHORT", "UP", "EMA_BULL"),
        ):
            sub = fwd[(fwd["direction"] == direction) & (fwd["ema_context"] == need_ctx)]
            long_short.append(
                summarize_group(
                    sub,
                    main_h,
                    symbol=symbol,
                    timeframe=tf,
                    side=side,
                    setup=f"{direction}+{need_ctx}",
                    trend_group="TREND_ALIGNED",
                )
            )
        for side, direction, need_ctx in (
            ("LONG", "DOWN", "EMA_BULL"),
            ("SHORT", "UP", "EMA_BEAR"),
        ):
            sub = fwd[(fwd["direction"] == direction) & (fwd["ema_context"] == need_ctx)]
            long_short.append(
                summarize_group(
                    sub,
                    main_h,
                    symbol=symbol,
                    timeframe=tf,
                    side=side,
                    setup=f"{direction}+{need_ctx}",
                    trend_group="COUNTERTREND",
                )
            )

        # Q4 within TREND_ALIGNED
        aligned = groups["TREND_ALIGNED"]
        for q in ("Q1", "Q2", "Q3", "Q4"):
            sub = aligned[aligned["eff_quantile"].astype(str) == q]
            eff_rows.append(
                summarize_group(
                    sub,
                    main_h,
                    symbol=symbol,
                    timeframe=tf,
                    trend_group="TREND_ALIGNED",
                    eff_quantile=q,
                    side="COMBINED",
                )
            )
        # TREND_ALIGNED all efficiencies summary already in comparison
        # COUNTERTREND + Q4
        ct_q4 = groups["COUNTERTREND"][
            groups["COUNTERTREND"]["eff_quantile"].astype(str) == "Q4"
        ]
        eff_rows.append(
            summarize_group(
                ct_q4,
                main_h,
                symbol=symbol,
                timeframe=tf,
                trend_group="COUNTERTREND",
                eff_quantile="Q4",
                side="COMBINED",
            )
        )
        al_q4 = aligned[aligned["eff_quantile"].astype(str) == "Q4"]
        # lifts Q4 vs aligned all / vs counter Q4
        al_all = summaries["TREND_ALIGNED"]
        al_q4_m = summarize_group(al_q4, main_h)
        ct_q4_m = summarize_group(ct_q4, main_h)
        eff_rows.append(
            {
                "symbol": symbol,
                "timeframe": tf,
                "side": "COMBINED",
                "trend_group": "LIFTS",
                "n": al_q4_m.get("n"),
                **add_lifts(al_q4_m, al_all, "q4_vs_aligned_all"),
                **add_lifts(al_q4_m, ct_q4_m, "aligned_q4_vs_counter_q4"),
                "aligned_q4_median_net": al_q4_m.get("median_net"),
                "aligned_all_median_net": al_all.get("median_net"),
                "counter_q4_median_net": ct_q4_m.get("median_net"),
            }
        )

        # monotonicity COMBINED + by side within TREND_ALIGNED
        for side_name, sub0 in (
            ("COMBINED", aligned),
            ("LONG", aligned[aligned["side"] == "LONG"]),
            ("SHORT", aligned[aligned["side"] == "SHORT"]),
        ):
            nets = []
            hits = []
            for q in ("Q1", "Q2", "Q3", "Q4"):
                s = sub0[sub0["eff_quantile"].astype(str) == q]
                m = summarize_group(s, main_h)
                nets.append(m.get("median_net"))
                hits.append(m.get("hit_rate"))
            mono_rows.append(
                {
                    "symbol": symbol,
                    "timeframe": tf,
                    "side": side_name,
                    "median_net_Q1": nets[0],
                    "median_net_Q2": nets[1],
                    "median_net_Q3": nets[2],
                    "median_net_Q4": nets[3],
                    "hit_Q1": hits[0],
                    "hit_Q2": hits[1],
                    "hit_Q3": hits[2],
                    "hit_Q4": hits[3],
                    "monotonicity_net": monotonicity_label(nets),
                    "monotonicity_hit": monotonicity_label(hits),
                }
            )

        # countertrend edge row
        counter_rows.append(
            {
                **summaries["COUNTERTREND"],
                "symbol": symbol,
                "timeframe": tf,
                "trend_group": "COUNTERTREND",
            }
        )

        # retention / opportunity
        n_all = max(1, int(len(fwd)))
        n_al = int(len(aligned))
        n_ct = int(len(groups["COUNTERTREND"]))
        retained = n_al / n_all
        retention.append(
            {
                "symbol": symbol,
                "timeframe": tf,
                "n_all": n_all,
                "n_trend_aligned": n_al,
                "n_countertrend": n_ct,
                "n_mixed": int(len(groups["MIXED"])),
                "retained_fraction": retained,
                "removed_fraction": 1.0 - retained,
            }
        )
        # signals per day / month
        et = pd.to_datetime(fwd["entry_time"], utc=True)
        span_days = max(1.0, (et.max() - et.min()).total_seconds() / 86400.0)
        retention[-1]["median_signals_per_day_all"] = float(n_all / span_days)
        retention[-1]["median_signals_per_day_aligned"] = float(n_al / span_days)

        opportunity.append(
            {
                "symbol": symbol,
                "timeframe": tf,
                "all_per_100_mean_net": 100.0 * (summaries["ALL"].get("mean_net") or 0.0),
                "aligned_per_100_all_events": retained
                * 100.0
                * (summaries["TREND_ALIGNED"].get("mean_net") or 0.0),
                "counter_per_100_all_events": (n_ct / n_all)
                * 100.0
                * (summaries["COUNTERTREND"].get("mean_net") or 0.0),
                "retained_fraction": retained,
                "all_mean_net": summaries["ALL"].get("mean_net"),
                "aligned_mean_net": summaries["TREND_ALIGNED"].get("mean_net"),
                "counter_mean_net": summaries["COUNTERTREND"].get("mean_net"),
            }
        )

        # monthly stability
        tmp = fwd.copy()
        tmp["month"] = pd.to_datetime(tmp["entry_time"], utc=True).dt.strftime("%Y-%m")
        for gname, mask in (
            ("ALL", pd.Series(True, index=tmp.index)),
            ("TREND_ALIGNED", tmp["trend_bucket"] == "TREND_ALIGNED"),
            ("COUNTERTREND", tmp["trend_bucket"] == "COUNTERTREND"),
            (
                "TREND_ALIGNED_Q4",
                (tmp["trend_bucket"] == "TREND_ALIGNED")
                & (tmp["eff_quantile"].astype(str) == "Q4"),
            ),
        ):
            sub0 = tmp[mask]
            month_nets = []
            for month, sub in sub0.groupby("month"):
                m = summarize_group(
                    sub,
                    main_h,
                    symbol=symbol,
                    timeframe=tf,
                    trend_group=gname,
                    month=month,
                    side="COMBINED",
                )
                monthly.append(m)
                if m.get("median_net") is not None and m.get("sample_flag") == "OK":
                    month_nets.append(m["median_net"])
            if month_nets:
                retention.append(
                    {
                        "symbol": symbol,
                        "timeframe": tf,
                        "trend_group": gname,
                        "metric": "monthly_summary",
                        "positive_month_share": float(np.mean([1 if x > 0 else 0 for x in month_nets])),
                        "median_monthly_net": float(np.median(month_nets)),
                        "worst_month_net": float(np.min(month_nets)),
                        "best_month_net": float(np.max(month_nets)),
                        "n_ok_months": int(len(month_nets)),
                    }
                )

    events = pd.concat(event_frames, ignore_index=True) if event_frames else pd.DataFrame()
    return {
        "events": events,
        "trend_filter_comparison": comparison,
        "trend_filter_long_short": long_short,
        "trend_aligned_efficiency_quartiles": eff_rows,
        "efficiency_monotonicity": mono_rows,
        "countertrend_edge": counter_rows,
        "signal_retention": [r for r in retention if "retained_fraction" in r or r.get("metric") == "monthly_summary"],
        "opportunity_adjusted": opportunity,
        "monthly_stability": monthly,
    }


def cross_symbol_consistency(doge: dict, btc: dict) -> list[dict]:
    rows = []
    doge_c = {
        (r.get("timeframe"), r.get("trend_group")): r
        for r in doge["trend_filter_comparison"]
        if r.get("side") == "COMBINED" or "LIFTS" in str(r.get("trend_group"))
    }
    btc_c = {
        (r.get("timeframe"), r.get("trend_group")): r
        for r in btc["trend_filter_comparison"]
        if r.get("side") == "COMBINED" or "LIFTS" in str(r.get("trend_group"))
    }

    def effect(lift: float | None) -> str:
        if lift is None:
            return "NA"
        if lift > 0.01:
            return "POSITIVE"
        if lift < -0.01:
            return "NEGATIVE"
        return "FLAT"

    def cons(a: str, b: str) -> str:
        if a == "NA" or b == "NA":
            return "INSUFFICIENT"
        if a == b and a != "FLAT":
            return "REPLICATES"
        if a == "FLAT" or b == "FLAT":
            return "MIXED"
        if a != b:
            return "CONTRADICTS"
        return "MIXED"

    for tf in SIGNAL_TFS:
        for key, metric in (
            ("TREND_ALIGNED_LIFTS_VS_ALL", "vs_ALL_net_return_lift"),
            ("TREND_ALIGNED_LIFTS_VS_COUNTERTREND", "vs_COUNTERTREND_net_return_lift"),
        ):
            d = doge_c.get((tf, key), {})
            b = btc_c.get((tf, key), {})
            de, be = effect(d.get(metric)), effect(b.get(metric))
            rows.append(
                {
                    "timeframe": tf,
                    "metric": key,
                    "DOGE_effect": de,
                    "BTC_effect": be,
                    "consistency": cons(de, be),
                    "DOGE_lift": d.get(metric),
                    "BTC_lift": b.get(metric),
                }
            )

    # Q4 vs aligned all from eff lifts rows
    for lab, bundle in (("DOGE", doge), ("BTC", btc)):
        pass
    doge_e = [r for r in doge["trend_aligned_efficiency_quartiles"] if r.get("trend_group") == "LIFTS"]
    btc_e = [r for r in btc["trend_aligned_efficiency_quartiles"] if r.get("trend_group") == "LIFTS"]
    for tf in SIGNAL_TFS:
        d = next((r for r in doge_e if r.get("timeframe") == tf), {})
        b = next((r for r in btc_e if r.get("timeframe") == tf), {})
        de = effect(d.get("q4_vs_aligned_all_net_return_lift"))
        be = effect(b.get("q4_vs_aligned_all_net_return_lift"))
        rows.append(
            {
                "timeframe": tf,
                "metric": "ALIGNED_Q4_vs_ALIGNED_ALL",
                "DOGE_effect": de,
                "BTC_effect": be,
                "consistency": cons(de, be),
                "DOGE_lift": d.get("q4_vs_aligned_all_net_return_lift"),
                "BTC_lift": b.get("q4_vs_aligned_all_net_return_lift"),
            }
        )
    return rows


def decide_primary(comparison: list[dict], retention: list[dict]) -> str:
    """
    MATERIALLY_IMPROVES: aligned net lift vs ALL >0.03 on majority TF×symbol and retained>=0.35
    IMPROVES_QUALITY_BUT_COSTS_COVERAGE: lift>0 but retained<0.45 or lift small
    ADDS_NO_ROBUST_VALUE: mixed/near-zero lifts
    HURTS: negative lift majority
    """
    lifts = [
        r
        for r in comparison
        if r.get("trend_group") == "TREND_ALIGNED_LIFTS_VS_ALL"
        and r.get("vs_ALL_net_return_lift") is not None
    ]
    if len(lifts) < 4:
        return "TREND_FILTER_ADDS_NO_ROBUST_VALUE"
    pos = sum(1 for r in lifts if (r.get("vs_ALL_net_return_lift") or 0) > 0.01)
    neg = sum(1 for r in lifts if (r.get("vs_ALL_net_return_lift") or 0) < -0.01)
    big = sum(1 for r in lifts if (r.get("vs_ALL_net_return_lift") or 0) > 0.03)
    rets = [r.get("retained_fraction") for r in retention if "retained_fraction" in r]
    med_ret = float(np.median([x for x in rets if x is not None])) if rets else 0.0
    if neg > pos:
        return "TREND_FILTER_HURTS_EDGE"
    if big >= max(3, len(lifts) // 2) and med_ret >= 0.35:
        return "TREND_FILTER_MATERIALLY_IMPROVES_WAVE_FADE"
    if pos > neg:
        return "TREND_FILTER_IMPROVES_QUALITY_BUT_COSTS_COVERAGE"
    return "TREND_FILTER_ADDS_NO_ROBUST_VALUE"


def decide_q4(eff_rows: list[dict]) -> str:
    lifts = [r for r in eff_rows if r.get("trend_group") == "LIFTS"]
    if len(lifts) < 4:
        return "Q4_NO_ADDED_VALUE"
    pos = sum(1 for r in lifts if (r.get("q4_vs_aligned_all_net_return_lift") or 0) > 0.01)
    big = sum(1 for r in lifts if (r.get("q4_vs_aligned_all_net_return_lift") or 0) > 0.03)
    neg = sum(1 for r in lifts if (r.get("q4_vs_aligned_all_net_return_lift") or 0) < -0.01)
    if big >= max(3, len(lifts) // 2) and pos > neg:
        return "Q4_ADDS_VALUE_WITHIN_TREND_ALIGNED"
    if pos > neg:
        return "Q4_ADDS_CONTEXT_ONLY"
    return "Q4_NO_ADDED_VALUE"


def decide_counter(counter_rows: list[dict]) -> str:
    ok = [r for r in counter_rows if r.get("sample_flag") == "OK"]
    if len(ok) < 4:
        return "COUNTERTREND_RESULT_MIXED"
    pos = sum(1 for r in ok if (r.get("median_net") or 0) > 0.02)
    neg = sum(1 for r in ok if (r.get("median_net") or 0) < -0.02)
    if pos >= max(3, len(ok) // 2) and pos > neg:
        return "COUNTERTREND_FADES_RETAIN_POSITIVE_EDGE"
    if neg >= max(3, len(ok) // 2) and neg > pos:
        return "COUNTERTREND_FADES_SHOULD_BE_BLOCKED"
    return "COUNTERTREND_RESULT_MIXED"


def run_analysis() -> dict[str, Any]:
    print(TREND_DOC, flush=True)
    edges = load_frozen_quantile_edges()

    print("[load] DOGE 1m", flush=True)
    doge_1m = load_1m(symbol="DOGEUSDT")
    print("[run] DOGEUSDT", flush=True)
    doge = run_symbol("DOGEUSDT", quantile_edges=edges, c1=doge_1m)

    print("[load] BTC 1m", flush=True)
    btc_1m = load_1m(symbol="BTCUSDT")
    b0, b1 = btc_entry_bounds(btc_1m)
    print(f"[btc] entry window {b0} -> {b1}", flush=True)
    btc = run_symbol(
        "BTCUSDT",
        quantile_edges=edges,
        c1=btc_1m,
        entry_start=b0,
        entry_end=b1,
    )

    events = pd.concat([doge["events"], btc["events"]], ignore_index=True)
    comparison = doge["trend_filter_comparison"] + btc["trend_filter_comparison"]
    long_short = doge["trend_filter_long_short"] + btc["trend_filter_long_short"]
    eff = doge["trend_aligned_efficiency_quartiles"] + btc["trend_aligned_efficiency_quartiles"]
    mono = doge["efficiency_monotonicity"] + btc["efficiency_monotonicity"]
    counter = doge["countertrend_edge"] + btc["countertrend_edge"]
    retention = doge["signal_retention"] + btc["signal_retention"]
    opportunity = doge["opportunity_adjusted"] + btc["opportunity_adjusted"]
    monthly = doge["monthly_stability"] + btc["monthly_stability"]
    cross = cross_symbol_consistency(doge, btc)

    primary = decide_primary(comparison, retention)
    q4_dec = decide_q4(eff)
    ct_dec = decide_counter(counter)

    return {
        "audit_version": AUDIT_VERSION,
        "source": SOURCE_GENERALIZATION,
        "fee_pct": FEE_PCT,
        "trend_definition": TREND_DOC.strip(),
        "events_with_trend": events,
        "trend_filter_comparison": comparison,
        "trend_filter_long_short": long_short,
        "trend_aligned_efficiency_quartiles": eff,
        "efficiency_monotonicity": mono,
        "countertrend_edge": counter,
        "signal_retention": retention,
        "opportunity_adjusted": opportunity,
        "monthly_stability": monthly,
        "cross_symbol_consistency": cross,
        "decisions": {
            "primary": primary,
            "q4": q4_dec,
            "countertrend": ct_dec,
        },
        "main_horizons": dict(MAIN_HORIZON_BY_TF),
        "gen_dir": str(GEN_DIR),
    }
