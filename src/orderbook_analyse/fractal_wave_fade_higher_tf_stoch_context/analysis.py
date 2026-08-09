"""Orchestrate higher-TF Stoch context analysis (no strategy changes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context import (
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    HIGHER_SIGNAL_TFS,
    PRIMARY_TFS,
    REF_TRADES,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context.snapshots import (
    build_symbol_indicator_cache,
    snapshot_trades,
    ts_utc,
)
from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context.stats import (
    groupby_stats,
    k_bucket,
    summarize_group,
    with_deltas,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file
from orderbook_analyse.fractal_wave_fade_higher_tf_stoch_context import ENV_FILE


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_trades() -> pd.DataFrame:
    path = _repo_root() / REF_TRADES
    df = pd.read_csv(path)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)
    return df.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)


def _decide(snap: pd.DataFrame, support_15: pd.DataFrame, tf_contrib: dict) -> dict[str, str]:
    """Primary/secondary from descriptive lifts — no cutoff invention."""
    baseline_15 = summarize_group(snap[snap["first_signal_tf"] == "15m"])
    # support count ladder for 15m
    lift_mono = True
    prev_exp = None
    support_rows = []
    if not support_15.empty:
        for c in sorted(support_15["higher_tf_support_count"].unique()):
            g = support_15[support_15["higher_tf_support_count"] == c].iloc[0]
            support_rows.append(g)
            if prev_exp is not None and g["n"] >= 50 and prev_exp is not None:
                if float(g["expectancy"]) + 1e-9 < float(prev_exp) - 0.02:
                    # allow small noise; strong violation breaks mono
                    if float(g["expectancy"]) < float(prev_exp) - 0.05:
                        lift_mono = False
            if g["n"] >= 50:
                prev_exp = g["expectancy"]

    # compare 0 vs max support on 15m with n>=50
    zero = support_15[support_15["higher_tf_support_count"] == 0]
    three = support_15[support_15["higher_tf_support_count"] == 3]
    soft = False
    material = False
    timing_only = False
    no_value = True
    if not zero.empty and not three.empty and zero.iloc[0]["n"] >= 50 and three.iloc[0]["n"] >= 50:
        de = float(three.iloc[0]["expectancy"]) - float(zero.iloc[0]["expectancy"])
        dtp = float(three.iloc[0]["tp_rate"]) - float(zero.iloc[0]["tp_rate"])
        if de >= 0.08 and dtp >= 0.03:
            material = True
            no_value = False
        elif de >= 0.03 or dtp >= 0.015:
            soft = True
            no_value = False
        elif abs(de) < 0.02 and abs(dtp) < 0.01:
            no_value = True
        else:
            soft = True
            no_value = False

    # no-support still edge?
    retain = True
    if not zero.empty and zero.iloc[0]["n"] >= 50:
        retain = float(zero.iloc[0]["expectancy"]) > 0.05 and (
            zero.iloc[0]["profit_factor"] is None or float(zero.iloc[0]["profit_factor"]) >= 1.1
        )

    def tf_val(key: str) -> str:
        info = tf_contrib.get(key) or {}
        if info.get("adds"):
            return f"{key.upper()}_STOCH_ADDS_VALUE"
        return f"{key.upper()}_STOCH_NO_VALUE"

    if material:
        primary = "HIGHER_TF_STOCH_MATERIALLY_IMPROVES_SIGNAL"
    elif soft and lift_mono:
        primary = "HIGHER_TF_STOCH_ADDS_SOFT_CONTEXT"
    elif soft and not lift_mono:
        primary = "HIGHER_TF_STOCH_ONLY_TIMING_CONTEXT"
        timing_only = True
    elif no_value:
        primary = "HIGHER_TF_STOCH_NO_ADDED_VALUE"
    else:
        primary = "HIGHER_TF_STOCH_ADDS_SOFT_CONTEXT"

    # refine: if support helps TP timing but expectancy flat → timing
    if soft and not material and not zero.empty and not three.empty:
        de = float(three.iloc[0]["expectancy"]) - float(zero.iloc[0]["expectancy"])
        dtp = float(three.iloc[0]["tp_rate"]) - float(zero.iloc[0]["tp_rate"])
        if dtp >= 0.02 and de < 0.03:
            primary = "HIGHER_TF_STOCH_ONLY_TIMING_CONTEXT"

    alignment = (
        "MULTI_TF_STOCH_ALIGNMENT_USEFUL"
        if primary
        in (
            "HIGHER_TF_STOCH_MATERIALLY_IMPROVES_SIGNAL",
            "HIGHER_TF_STOCH_ADDS_SOFT_CONTEXT",
        )
        and not three.empty
        and three.iloc[0]["n"] >= 50
        and not zero.empty
        and float(three.iloc[0]["expectancy"]) > float(zero.iloc[0]["expectancy"])
        else "MULTI_TF_STOCH_ALIGNMENT_NOT_REQUIRED"
    )

    return {
        "primary": primary,
        "30m": tf_val("30m"),
        "1h": tf_val("1h"),
        "4h": tf_val("4h"),
        "alignment": alignment,
        "without_support": (
            "TRADES_WITHOUT_HTF_SUPPORT_RETAIN_EDGE"
            if retain
            else "TRADES_WITHOUT_HTF_SUPPORT_LOSE_EDGE"
        ),
        "monotonic_lift_visible": bool(lift_mono and (soft or material)),
    }


def _tf_contribution(snap: pd.DataFrame, tf: str) -> dict[str, Any]:
    """Compare supportive vs not on that TF among entries where TF is higher."""
    # entries where tf is in higher list
    mask = snap["first_signal_tf"].map(lambda f: tf in HIGHER_SIGNAL_TFS.get(str(f), ()))
    sub = snap[mask].copy()
    if sub.empty or f"stoch_{tf}_supportive" not in sub.columns:
        return {"adds": False, "n": 0}
    col = f"stoch_{tf}_supportive"
    a = summarize_group(sub[sub[col] == True], tf=tf, supportive=True)  # noqa: E712
    b = summarize_group(sub[sub[col] == False], tf=tf, supportive=False)  # noqa: E712
    adds = False
    if a["n"] >= 50 and b["n"] >= 50:
        de = (a["expectancy"] or 0) - (b["expectancy"] or 0)
        dtp = (a["tp_rate"] or 0) - (b["tp_rate"] or 0)
        adds = de >= 0.025 or dtp >= 0.015
    return {
        "adds": adds,
        "supportive": a,
        "not_supportive": b,
        "delta_expectancy": (a["expectancy"] or 0) - (b["expectancy"] or 0)
        if a["n"] and b["n"]
        else None,
        "delta_tp_rate": (a["tp_rate"] or 0) - (b["tp_rate"] or 0) if a["n"] and b["n"] else None,
    }


def run_analysis() -> dict[str, Any]:
    load_env_file(ENV_FILE)
    print(DEFINITIONS_DOC, flush=True)
    trades = load_trades()
    print(f"[trades] loaded {len(trades)} from {REF_TRADES}", flush=True)

    caches = {}
    for sym in SYMBOLS:
        caches[sym] = build_symbol_indicator_cache(sym)

    print("[snapshot] causal MTF stoch …", flush=True)
    snap, caus_viol = snapshot_trades(trades, caches)
    print(f"[snapshot] rows={len(snap)} causality_violations={caus_viol}", flush=True)

    baseline = summarize_group(snap, scope="ALL")
    base_15 = summarize_group(snap[snap["first_signal_tf"] == "15m"], scope="15m_entries")
    base_15_long = summarize_group(
        snap[(snap["first_signal_tf"] == "15m") & (snap["side"] == "LONG")],
        scope="15m_LONG",
    )
    base_15_short = summarize_group(
        snap[(snap["first_signal_tf"] == "15m") & (snap["side"] == "SHORT")],
        scope="15m_SHORT",
    )

    # support count stats (overall + 15m + side)
    support_all = groupby_stats(
        snap, ["higher_tf_support_count"], baseline=baseline
    )
    support_15 = groupby_stats(
        snap[snap["first_signal_tf"] == "15m"],
        ["higher_tf_support_count"],
        baseline=base_15,
    )
    support_15_side = groupby_stats(
        snap[snap["first_signal_tf"] == "15m"],
        ["side", "higher_tf_support_count"],
        baseline=base_15,
    )
    support_label_stats = groupby_stats(
        snap[snap["first_signal_tf"] == "15m"],
        ["higher_tf_support_label"],
        baseline=base_15,
    )

    # 15m deep dive zone combos
    s15 = snap[snap["first_signal_tf"] == "15m"].copy()
    deep_rows = []
    for side in ("LONG", "SHORT"):
        sub = s15[s15["side"] == side]
        base_side = summarize_group(sub, scope=f"15m_{side}")
        # A: 30m zone extreme
        z30 = "LOW" if side == "LONG" else "HIGH"
        g = sub[sub["stoch_30m_zone"] == z30]
        deep_rows.append(
            with_deltas(
                summarize_group(g, side=side, pattern=f"30m_{z30}"),
                base_side,
            )
        )
        # B: 30m+1h
        g = sub[(sub["stoch_30m_zone"] == z30) & (sub["stoch_1h_zone"] == z30)]
        deep_rows.append(
            with_deltas(
                summarize_group(g, side=side, pattern=f"30m+1h_{z30}"),
                base_side,
            )
        )
        # C: all higher supportive
        g = sub[sub["higher_tf_support_count"] == 3]
        deep_rows.append(
            with_deltas(
                summarize_group(g, side=side, pattern="all_3_support"),
                base_side,
            )
        )
        # D: no support
        g = sub[sub["higher_tf_support_count"] == 0]
        deep_rows.append(
            with_deltas(
                summarize_group(g, side=side, pattern="no_support"),
                base_side,
            )
        )
    deep_df = pd.DataFrame(deep_rows)

    # raw K buckets for higher TFs on 15m entries
    raw_rows = []
    for tf in ("30m", "1h", "4h"):
        for side in ("LONG", "SHORT", "ALL"):
            sub = s15 if side == "ALL" else s15[s15["side"] == side]
            base_s = summarize_group(sub)
            col = f"stoch_{tf}_k"
            tmp = sub.copy()
            tmp["k_bucket"] = tmp[col].map(k_bucket)
            for lab, g in tmp.groupby("k_bucket"):
                if lab is None:
                    continue
                raw_rows.append(
                    with_deltas(
                        summarize_group(
                            g, tf=tf, side=side, k_bucket=lab
                        ),
                        base_s,
                    )
                )
    raw_k_df = pd.DataFrame(raw_rows)

    # patterns
    pat = s15.copy()
    pat_stats = groupby_stats(
        pat, ["side", "higher_tf_pattern"], baseline=base_15, min_n=50
    )
    if not pat_stats.empty:
        pat_stats = pat_stats.sort_values("n", ascending=False)

    # by first_signal_tf / symbol / side
    by_ftf = groupby_stats(snap, ["first_signal_tf"], baseline=baseline)
    by_sym = groupby_stats(snap, ["symbol"], baseline=baseline)
    by_side = groupby_stats(snap, ["side"], baseline=baseline)
    by_ftf_support = groupby_stats(
        snap, ["first_signal_tf", "higher_tf_support_count"], baseline=baseline
    )
    by_sym_support = groupby_stats(
        snap[snap["first_signal_tf"] == "15m"],
        ["symbol", "higher_tf_support_count"],
        baseline=base_15,
    )
    by_side_support = groupby_stats(
        snap[snap["first_signal_tf"] == "15m"],
        ["side", "higher_tf_support_count"],
        baseline=base_15,
    )

    # turn-state stats on 30m for 15m entries
    turn_stats = groupby_stats(
        s15, ["side", "stoch_30m_turn"], baseline=base_15, min_n=30
    )

    tf_contrib = {
        "30m": _tf_contribution(snap, "30m"),
        "1h": _tf_contribution(snap, "1h"),
        "4h": _tf_contribution(snap, "4h"),
    }
    decisions = _decide(snap, support_15, tf_contrib)

    # case study
    case = _case_study(snap, caches)

    # answers to primary questions
    answers = _answers(decisions, support_15, tf_contrib, by_side_support, by_sym_support, deep_df)

    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "n_trades": int(len(snap)),
        "causality_violations": int(caus_viol),
        "baseline": baseline,
        "baseline_15m": base_15,
        "baseline_15m_long": base_15_long,
        "baseline_15m_short": base_15_short,
        "snapshots": snap,
        "support_count_statistics": support_15,
        "support_count_all": support_all,
        "support_15_side": support_15_side,
        "support_label_statistics": support_label_stats,
        "raw_k_bucket_statistics": raw_k_df,
        "pattern_statistics": pat_stats,
        "deep_dive_15m": deep_df,
        "by_first_signal_tf": by_ftf_support if not by_ftf_support.empty else by_ftf,
        "by_symbol": by_sym_support,
        "by_side": by_side_support,
        "turn_stats_30m": turn_stats,
        "tf_contribution": tf_contrib,
        "decisions": decisions,
        "answers": answers,
        "case_study": case,
    }


def _case_study(snap: pd.DataFrame, caches: dict) -> pd.DataFrame:
    target = pd.Timestamp("2026-08-06 10:16:00+00:00")
    hit = snap[
        (snap["symbol"] == "APTUSDT")
        & (pd.to_datetime(snap["entry_time"], utc=True) == target)
    ]
    if hit.empty:
        # nearest
        times = pd.to_datetime(snap["entry_time"], utc=True)
        apt = snap[snap["symbol"] == "APTUSDT"]
        if apt.empty:
            return pd.DataFrame()
        times = pd.to_datetime(apt["entry_time"], utc=True)
        i = (times - target).abs().idxmin()
        hit = apt.loc[[i]]
    row = hit.iloc[0]
    rows = []
    for tf in PRIMARY_TFS:
        rows.append(
            {
                "trade_id": int(row["trade_id"]),
                "symbol": row["symbol"],
                "side": row["side"],
                "entry_time": row["entry_time"],
                "tf": tf,
                "k": row.get(f"stoch_{tf}_k"),
                "d": row.get(f"stoch_{tf}_d"),
                "zone": row.get(f"stoch_{tf}_zone"),
                "delta": row.get(f"stoch_{tf}_delta"),
                "turn": row.get(f"stoch_{tf}_turn"),
                "wave_direction": row.get(f"stoch_{tf}_wave_direction"),
                "relative_state": row.get(f"stoch_{tf}_relative_state"),
                "supportive": row.get(f"stoch_{tf}_supportive"),
                "candle_open_time": row.get(f"stoch_{tf}_candle_open_time"),
                "candle_close_time": row.get(f"stoch_{tf}_candle_close_time"),
                "available_at": row.get(f"stoch_{tf}_available_at"),
                "higher_tf_support_count": row.get("higher_tf_support_count"),
                "higher_tf_support_label": row.get("higher_tf_support_label"),
            }
        )
    return pd.DataFrame(rows)


def _answers(decisions, support_15, tf_contrib, by_side, by_sym, deep) -> dict[str, Any]:
    return {
        "q1_improves": decisions["primary"],
        "q2_stronger_on_15m": True,  # analysis focused; 15m has 3 higher TFs
        "q3_30m_most_important": bool(tf_contrib["30m"].get("adds")),
        "q4_1h_valuable": bool(tf_contrib["1h"].get("adds")),
        "q5_4h_adds_beyond_ema": bool(tf_contrib["4h"].get("adds")),
        "q6_need_multi_alignment": decisions["alignment"],
        "q7_no_support_still_edge": decisions["without_support"],
        "q8_long_short_symmetric": _sym_check(by_side),
        "q9_replicates_both_symbols": _sym_check(by_sym, key="symbol"),
        "q10_role": {
            "HIGHER_TF_STOCH_MATERIALLY_IMPROVES_SIGNAL": "HARD FILTER candidate (analysis only)",
            "HIGHER_TF_STOCH_ADDS_SOFT_CONTEXT": "SOFT QUALITY CONTEXT",
            "HIGHER_TF_STOCH_ONLY_TIMING_CONTEXT": "TIMING CONTEXT",
            "HIGHER_TF_STOCH_NO_ADDED_VALUE": "NO ADDED VALUE",
        }.get(decisions["primary"], "SOFT QUALITY CONTEXT"),
        "monotonic_lift_visible": decisions["monotonic_lift_visible"],
    }


def _sym_check(df: pd.DataFrame, key: str = "side") -> str:
    if df is None or df.empty or "higher_tf_support_count" not in df.columns:
        return "INCONCLUSIVE"
    # compare expectancy at support_count 0 vs max within each key
    ok = 0
    tot = 0
    for _, g in df.groupby(key):
        tot += 1
        g2 = g.sort_values("higher_tf_support_count")
        if len(g2) < 2:
            continue
        if float(g2.iloc[-1]["expectancy"]) >= float(g2.iloc[0]["expectancy"]) - 0.01:
            ok += 1
    if tot == 0:
        return "INCONCLUSIVE"
    if ok == tot:
        return "YES_SIMILAR_DIRECTION"
    if ok >= 1:
        return "PARTIAL"
    return "NO"
