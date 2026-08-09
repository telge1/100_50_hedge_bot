"""Orchestrate July SL loser audit."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_loser_audit import (
    AUDIT_VERSION,
    DEFINITIONS_DOC,
    OUT_DIR_DEFAULT,
)
from orderbook_analyse.fractal_wave_fade_loser_audit.diagnostics import (
    assign_failure_mode,
    build_signal_index,
    htf_momentum,
    levels,
    load_1m,
    load_july_trades,
    load_tf_frames,
    mark_repeated_fades,
    match_signal,
    path_diagnostics,
    pre_entry_moves,
    stoch_failure_flags,
)


def _cluster_streaks(july_ns: list[int]) -> list[dict[str, Any]]:
    """Find consecutive july_n SL runs (gaps of 1 in trade numbering among SLs that are consecutive trades)."""
    # streaks among sequential july trade numbers all being SL
    # We'll pass full july outcome list instead
    return []


def find_sl_clusters(jul: pd.DataFrame) -> pd.DataFrame:
    """Clusters where consecutive july trades are all SL (length>=2)."""
    rows = []
    i = 0
    n = len(jul)
    while i < n:
        if jul.iloc[i]["exit_reason"] != "SL":
            i += 1
            continue
        j = i
        while j < n and jul.iloc[j]["exit_reason"] == "SL":
            j += 1
        length = j - i
        if length >= 2:
            sub = jul.iloc[i:j]
            rows.append(
                {
                    "cluster_start_july_n": int(sub.iloc[0]["july_n"]),
                    "cluster_end_july_n": int(sub.iloc[-1]["july_n"]),
                    "length": int(length),
                    "symbols": ",".join(sub["symbol"].astype(str).unique()),
                    "sides": ",".join(sub["side"].astype(str).unique()),
                    "signal_tfs": ",".join(sub["first_signal_tf"].astype(str).unique()),
                    "trade_ids": ",".join(sub["trade_id"].astype(str)),
                }
            )
        i = j
    return pd.DataFrame(rows)


def pattern_stats(losers: pd.DataFrame, winners: pd.DataFrame, col: str, value) -> dict[str, Any]:
    sl_n = int(len(losers))
    win_n = int(len(winners))
    sl_hit = int((losers[col] == value).sum()) if col in losers.columns else 0
    win_hit = int((winners[col] == value).sum()) if col in winners.columns else 0
    # among all trades with feature
    both = pd.concat([losers, winners], ignore_index=True)
    with_feat = both[both[col] == value] if col in both.columns else both.iloc[0:0]
    sl_rate = float((with_feat["exit_reason"] == "SL").mean()) if len(with_feat) else None
    without = both[both[col] != value] if col in both.columns else both
    sl_rate_wo = float((without["exit_reason"] == "SL").mean()) if len(without) else None
    return {
        "feature": f"{col}={value}",
        "sl_with_feature": sl_hit,
        "sl_total": sl_n,
        "p_feature_given_sl": float(sl_hit / sl_n) if sl_n else None,
        "winners_with_feature": win_hit,
        "winners_total": win_n,
        "p_feature_given_win": float(win_hit / win_n) if win_n else None,
        "p_sl_given_feature": sl_rate,
        "p_sl_without_feature": sl_rate_wo,
        "lift": (sl_rate / sl_rate_wo) if sl_rate is not None and sl_rate_wo and sl_rate_wo > 0 else None,
    }


def decide(losers: pd.DataFrame, patterns: pd.DataFrame, failure_counts: dict) -> dict[str, Any]:
    imm = int((losers["mfe_class"] == "IMMEDIATE_FAILURE").sum())
    near = int((losers["mfe_class"] == "NEAR_TP_THEN_FAIL").sum())
    partial = int((losers["mfe_class"] == "PARTIAL_FADE_THEN_FAIL").sum())
    dominant = max(failure_counts, key=failure_counts.get) if failure_counts else "OTHER"

    # Only count causal pre-entry features as filterable (exclude outcome labels)
    filterable = []
    skip = {
        "mfe_class=IMMEDIATE_FAILURE",
        "immediate_adverse=True",
        "htf_class=AGAINST_HTF_MOMENTUM",
        "stoch_pinned_extreme=True",
        "side=SHORT",
        "side=LONG",
    }
    if patterns is not None and len(patterns):
        for _, r in patterns.iterrows():
            feat = str(r["feature"])
            if feat in skip or feat.startswith("mfe_class="):
                continue
            lift = r.get("lift")
            p_sl = r.get("p_feature_given_sl")
            p_win = r.get("p_feature_given_win")
            if (
                lift is not None
                and lift >= 1.3
                and p_sl is not None
                and p_sl >= 0.25
                and (p_win is None or p_win <= 0.45)
            ):
                filterable.append(feat)

    if len(filterable) >= 1:
        decision = "LOSSES_HAVE_CLEAR_FILTERABLE_PATTERN"
    elif near >= 0.4 * len(losers):
        decision = "LOSSES_MAINLY_EXIT_MANAGEMENT_PROBLEM"
    elif failure_counts.get("REPEATED_FADE_IN_SAME_MOVE", 0) >= 0.35 * len(losers):
        decision = "LOSSES_MAINLY_REPEATED_FADE_FAILURES"
    elif (
        failure_counts.get("COUNTERTREND_CONTINUATION", 0) >= 0.45 * len(losers)
        and "htf_class=AGAINST_HTF_MOMENTUM" in filterable
    ):
        decision = "LOSSES_MAINLY_COUNTERTREND_FAILURES"
    else:
        decision = "LOSSES_HAVE_MULTIPLE_PARTIAL_PATTERNS"

    return {
        "decision": decision,
        "dominant_failure_mode": dominant,
        "immediate_failures": imm,
        "partial_fade_then_fail": partial,
        "near_tp_then_fail": near,
        "filterable_candidates": filterable,
    }


def run_analysis(*, out_dir: Path = OUT_DIR_DEFAULT) -> dict[str, Any]:
    jul = load_july_trades()
    losers_raw = jul[jul["exit_reason"] == "SL"].copy()
    winners_raw = jul[jul["exit_reason"] == "TP"].copy()
    symbols = sorted(jul["symbol"].unique().tolist())
    tfs = sorted(jul["first_signal_tf"].unique().tolist())
    # also need 1h/4h for HTF
    tf_all = sorted(set(tfs) | {"1h", "4h", "15m", "30m"})

    sig_index = build_signal_index(symbols, tfs)
    frames = load_tf_frames(symbols, tf_all)
    c1m = load_1m(symbols, jul["entry_time"].min(), jul["exit_time"].max())

    def enrich(df: pd.DataFrame) -> list[dict[str, Any]]:
        rows = []
        for _, tr in df.iterrows():
            lev = levels(tr)
            sig = match_signal(tr, sig_index)
            path = path_diagnostics(tr, c1m[str(tr["symbol"])], lev)
            pre = pre_entry_moves(tr, frames)
            htf = htf_momentum(tr, frames)
            stf = stoch_failure_flags(sig, str(tr["side"]))
            rows.append(
                {
                    "july_n": int(tr["july_n"]),
                    "trade_id": int(tr["trade_id"]),
                    "symbol": str(tr["symbol"]),
                    "side": str(tr["side"]),
                    "exit_reason": str(tr["exit_reason"]),
                    "entry_time": tr["entry_time"],
                    "exit_time": tr["exit_time"],
                    "signal_time": tr["signal_time"],
                    "entry_price": float(tr["entry_price"]),
                    "exit_price": float(tr["exit_price"]),
                    "net_return_pct": float(tr["net_return_pct"]),
                    "first_signal_tf": str(tr["first_signal_tf"]),
                    "highest_tf_reached": str(tr["highest_tf_reached"]),
                    "upgrade_count": int(tr["upgrade_count"]),
                    "upgrade_sequence": str(tr["upgrade_sequence"]),
                    **lev,
                    **sig,
                    **path,
                    **pre,
                    **htf,
                    **stf,
                    "bos_choch": "UNKNOWN",
                    "structure": "UNKNOWN",
                }
            )
        mark_repeated_fades(rows)
        for r in rows:
            r["failure_mode"] = assign_failure_mode(
                mfe_class=str(r.get("mfe_class")),
                htf_class=str(r.get("htf_class")),
                ext_bucket=str(r.get("extension_bucket")),
                stoch_failed=bool(r.get("failed_fade_stoch_pattern")),
                repeated_fade=bool(r.get("repeated_fade_same_move")),
                immediate_adverse=r.get("immediate_adverse"),
            )
            # short diagnosis text
            r["diagnosis"] = (
                f"{r['signal_type']} on {r['first_signal_tf']} ({r['wave_direction']} wave → {r['side']}); "
                f"MFE={r.get('mfe_pct')} ({r.get('mfe_class')}); HTF={r.get('htf_class')}; "
                f"ext={r.get('extension_bucket')}; mode={r['failure_mode']}"
            )
        return rows

    print("[enrich] losers …", flush=True)
    loser_rows = enrich(losers_raw)
    print("[enrich] winners (control) …", flush=True)
    winner_rows = enrich(winners_raw)
    losers = pd.DataFrame(loser_rows)
    winners = pd.DataFrame(winner_rows)

    # clusters
    clusters = find_sl_clusters(jul)
    # detail for known clusters
    cluster_notes = []
    for start, end in ((9, 13), (32, 34), (36, 37), (57, 58)):
        sub = losers[(losers["july_n"] >= start) & (losers["july_n"] <= end)]
        if sub.empty:
            continue
        cluster_notes.append(
            {
                "focus": f"#{start}-{end}",
                "n": int(len(sub)),
                "sides": sub["side"].value_counts().to_dict(),
                "symbols": sub["symbol"].value_counts().to_dict(),
                "signal_types": sub["signal_type"].value_counts().to_dict(),
                "tfs": sub["first_signal_tf"].value_counts().to_dict(),
                "htf": sub["htf_class"].value_counts().to_dict(),
                "mfe_class": sub["mfe_class"].value_counts().to_dict(),
                "failure_mode": sub["failure_mode"].value_counts().to_dict(),
                "repeated_fade": int(sub["repeated_fade_same_move"].sum()),
                "mean_pre_6": float(sub["pre_6_ret_pct"].dropna().mean()) if sub["pre_6_ret_pct"].notna().any() else None,
            }
        )

    # frequency tables
    signal_types = (
        losers.groupby(["signal_type", "side", "symbol", "first_signal_tf", "tpsl_profile"], dropna=False)
        .size()
        .reset_index(name="n")
    )
    signal_types["share_pct"] = 100.0 * signal_types["n"] / len(losers)

    failure_modes = losers["failure_mode"].value_counts().rename_axis("failure_mode").reset_index(name="n")
    failure_modes["share_pct"] = 100.0 * failure_modes["n"] / len(losers)
    failure_counts = losers["failure_mode"].value_counts().to_dict()

    mfe_tbl = losers["mfe_class"].value_counts().rename_axis("mfe_class").reset_index(name="n")
    mfe_tbl["share_pct"] = 100.0 * mfe_tbl["n"] / len(losers)
    # MFE thresholds reached
    mfe_reach = {
        "mfe_ge_25pct_tp": int((losers["mfe_to_tp"].fillna(0) >= 0.25).sum()),
        "mfe_ge_50pct_tp": int((losers["mfe_to_tp"].fillna(0) >= 0.50).sum()),
        "mfe_ge_75pct_tp": int((losers["mfe_to_tp"].fillna(0) >= 0.75).sum()),
        "immediate_failure": int((losers["mfe_class"] == "IMMEDIATE_FAILURE").sum()),
        "partial": int((losers["mfe_class"] == "PARTIAL_FADE_THEN_FAIL").sum()),
        "near_tp": int((losers["mfe_class"] == "NEAR_TP_THEN_FAIL").sum()),
    }

    # candidate patterns from loser hypotheses
    feats = []
    for col, val in [
        ("htf_class", "AGAINST_HTF_MOMENTUM"),
        ("htf_class", "WITH_HTF_MOMENTUM"),
        ("mfe_class", "IMMEDIATE_FAILURE"),
        ("extension_bucket", "STRONG_EXT"),
        ("extension_bucket", "WEAK_EXT"),
        ("stoch_pinned_extreme", True),
        ("failed_fade_stoch_pattern", True),
        ("repeated_fade_same_move", True),
        ("immediate_adverse", True),
        ("side", "SHORT"),
        ("side", "LONG"),
        ("symbol", "APTUSDT"),
        ("symbol", "DOGEUSDT"),
        ("first_signal_tf", "15m"),
        ("signal_type", "BEARISH_WAVE_FADE"),
        ("signal_type", "BULLISH_WAVE_FADE"),
    ]:
        # bool columns
        if col in losers.columns:
            feats.append(pattern_stats(losers, winners, col, val))
    patterns = pd.DataFrame(feats)
    patterns = patterns.sort_values("lift", ascending=False, na_position="last")

    # top 5 patterns for summary
    top5 = []
    for _, r in patterns.head(8).iterrows():
        if r["feature"] in ("side=SHORT", "side=LONG", "symbol=APTUSDT", "symbol=DOGEUSDT"):
            continue  # structural base rates, not filter patterns
        top5.append(
            {
                "name": r["feature"],
                "sl_affected": f"{int(r['sl_with_feature'])}/{int(r['sl_total'])}",
                "winners_affected": f"{int(r['winners_with_feature'])}/{int(r['winners_total'])}",
                "sl_rate_with": r["p_sl_given_feature"],
                "sl_rate_without": r["p_sl_without_feature"],
                "lift": r["lift"],
                "filter_candidate": (
                    "JA"
                    if (r.get("lift") or 0) >= 1.25
                    and (r.get("p_feature_given_sl") or 0) >= 0.3
                    and (r.get("p_feature_given_win") or 1) <= 0.5
                    else ("UNSICHER" if (r.get("lift") or 0) >= 1.15 else "NEIN")
                ),
            }
        )
        if len(top5) >= 5:
            break

    decision = decide(losers, patterns, failure_counts)

    # answers 1-10
    most_common_sig = losers["signal_type"].value_counts().index[0]
    answers = {
        "q1_most_common_loss_pattern": top5[0]["name"] if top5 else decision["dominant_failure_mode"],
        "q2_sls_affected": top5[0]["sl_affected"] if top5 else None,
        "q3_same_in_winners": top5[0]["winners_affected"] if top5 else None,
        "q4_signal_type_most_sls": most_common_sig,
        "q4_count": int((losers["signal_type"] == most_common_sig).sum()),
        "q5_short_vs_long": {
            "SHORT": int((losers["side"] == "SHORT").sum()),
            "LONG": int((losers["side"] == "LONG").sum()),
            "short_imm_share": float(
                (losers.loc[losers.side == "SHORT", "mfe_class"] == "IMMEDIATE_FAILURE").mean()
            )
            if (losers.side == "SHORT").any()
            else None,
            "long_imm_share": float(
                (losers.loc[losers.side == "LONG", "mfe_class"] == "IMMEDIATE_FAILURE").mean()
            )
            if (losers.side == "LONG").any()
            else None,
        },
        "q6_doge_vs_apt": losers["symbol"].value_counts().to_dict(),
        "q7_cluster_9_13": next((c for c in cluster_notes if c["focus"] == "#9-13"), None),
        "q8_immediate_failures": mfe_reach["immediate_failure"],
        "q9_mfe_thresholds": mfe_reach,
        "q10_simple_causal_filter": (
            top5[0]["name"] + " — candidate " + top5[0]["filter_candidate"]
            if top5
            else "none clear"
        ),
    }

    payload = {
        "audit_version": AUDIT_VERSION,
        "july_n_trades": int(len(jul)),
        "n_sl": int(len(losers)),
        "n_tp": int(len(winners)),
        "losers": losers,
        "winners": winners,
        "signal_types": signal_types,
        "failure_modes": failure_modes,
        "mfe_tbl": mfe_tbl,
        "mfe_reach": mfe_reach,
        "clusters": clusters,
        "cluster_notes": cluster_notes,
        "patterns": patterns,
        "top5": top5,
        "decision": decision,
        "answers": answers,
        "out_dir": out_dir,
        "definitions": DEFINITIONS_DOC,
    }
    return payload
