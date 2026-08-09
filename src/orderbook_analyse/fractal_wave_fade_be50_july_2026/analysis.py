"""Orchestrate BE50 July replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE
from orderbook_analyse.fractal_wave_fade_be50_july_2026 import (
    AUDIT_VERSION,
    FEE_PCT,
    OUT_DIR_DEFAULT,
    REF_TRADES,
    START_ACTIVE,
)
from orderbook_analyse.fractal_wave_fade_be50_july_2026.equity import simulate_equity_path
from orderbook_analyse.fractal_wave_fade_be50_july_2026.simulate import (
    simulate_be50_trade,
    trade_levels,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def load_july() -> pd.DataFrame:
    t = pd.read_csv(REF_TRADES)
    for c in ("entry_time", "exit_time", "signal_time"):
        t[c] = pd.to_datetime(t[c], utc=True)
    jul = t[(t["entry_time"] >= "2026-07-01") & (t["entry_time"] < "2026-08-01")].copy()
    jul = jul.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    jul["july_n"] = np.arange(1, len(jul) + 1)
    return jul


def load_1m(symbols, start, end) -> dict[str, pd.DataFrame]:
    load_env_file(ENV_FILE)
    out = {}
    for sym in symbols:
        print(f"[1m] {sym} …", flush=True)
        c = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        ts = pd.to_datetime(c["timestamp"], utc=True)
        c = c.loc[(ts >= start - pd.Timedelta(days=1)) & (ts <= end + pd.Timedelta(days=12))].reset_index(
            drop=True
        )
        out[sym] = c
    return out


def classify_change(base_reason: str, be_reason: str, base_net: float, be_net: float) -> str:
    if base_reason == "SL" and be_reason == "BE":
        return "SL_TO_BE"
    if base_reason == "TP" and be_reason == "BE":
        return "TP_TO_BE"
    if base_reason == "TP" and be_reason == "TP":
        return "UNCHANGED_TP"
    if base_reason == "SL" and be_reason == "SL":
        return "UNCHANGED_SL"
    if base_reason == be_reason:
        return f"UNCHANGED_{base_reason}"
    return f"{base_reason}_TO_{be_reason}"


def tp_group(net: float) -> str:
    # approx from gross-fee profiles: 0.89, 1.89, 3.89
    if abs(net - 0.89) < 0.05 or abs(net + 1.11) < 0.05:
        # losers with -1.11 are TP1/SL1; winners +0.89
        return "TP~1.0/SL~1.0"
    if abs(net - 1.89) < 0.05 or abs(net + 1.61) < 0.05:
        return "TP~2.0/SL~1.5"
    if abs(net - 3.89) < 0.05:
        return "TP~4.0/SL~2.0"
    # fallback by |net| buckets for winners/losers
    a = abs(net)
    if a < 1.3:
        return "TP~1.0/SL~1.0"
    if a < 2.5:
        return "TP~2.0/SL~1.5"
    return "TP~4.0/SL~2.0"


def decide(base_sum: dict, be_sum: dict, counts: dict) -> str:
    d_eq = be_sum["end_total"] - base_sum["end_total"]
    d_perf = be_sum["performance_pct"] - base_sum["performance_pct"]
    d_dd = be_sum["max_dd_pct"] - base_sum["max_dd_pct"]  # less negative is better
    n_amb = counts.get("n_ambiguous", 0)
    if n_amb >= 10:
        return "BE50_RESULT_AMBIGUOUS_DUE_TO_INTRABAR_DATA"
    if d_eq >= 50 and d_perf >= 5 and counts["SL_TO_BE"] > counts["TP_TO_BE"]:
        return "BE50_STRONGLY_IMPROVES_JULY"
    if d_eq >= 15 and d_perf >= 1.5 and counts["SL_TO_BE"] >= counts["TP_TO_BE"]:
        return "BE50_MODESTLY_IMPROVES_JULY"
    if abs(d_eq) < 10 and abs(d_perf) < 1.0:
        return "BE50_NEUTRAL"
    if d_eq <= -50 or (counts["TP_TO_BE"] > counts["SL_TO_BE"] * 1.5 and d_eq < 0):
        return "BE50_STRONGLY_HURTS_JULY"
    if d_eq < -10:
        return "BE50_HURTS_JULY"
    if d_eq > 0:
        return "BE50_MODESTLY_IMPROVES_JULY"
    return "BE50_NEUTRAL"


def run_analysis(*, out_dir: Path = OUT_DIR_DEFAULT) -> dict[str, Any]:
    jul = load_july()
    books = load_1m(
        sorted(jul["symbol"].unique()),
        jul["entry_time"].min(),
        jul["exit_time"].max(),
    )

    rows = []
    print(f"[sim] BE50 on {len(jul)} July trades …", flush=True)
    for _, tr in jul.iterrows():
        lev = trade_levels(tr)
        be = simulate_be50_trade(tr, books[str(tr["symbol"])], lev)
        base_net = float(tr["net_return_pct"])
        base_reason = str(tr["exit_reason"])
        be_net = be["be50_net_pct"]
        be_reason = be["be50_reason"]
        change = classify_change(base_reason, be_reason, base_net, be_net if be_net is not None else 0)
        # later TP under baseline?
        later_tp = None
        if change == "TP_TO_BE":
            later_tp = True  # baseline was TP so yes would have hit TP later
        rows.append(
            {
                "july_n": int(tr["july_n"]),
                "trade_id": int(tr["trade_id"]),
                "time": tr["entry_time"],
                "symbol": str(tr["symbol"]),
                "side": str(tr["side"]),
                "entry": lev["entry"],
                "original_tp": lev["tp"],
                "original_sl": lev["sl"],
                "be50_trigger_price": lev["be_trigger"],
                "tp_pct": lev["tp_pct"],
                "sl_pct": lev["sl_pct"],
                "baseline_reason": base_reason,
                "baseline_net_pct": base_net,
                "baseline_exit_time": tr["exit_time"],
                "be50_triggered": be["be50_triggered"],
                "be50_trigger_time": be["be50_trigger_time"],
                "be50_exit_time": be["be50_exit_time"],
                "be50_exit_price": be["be50_exit_price"],
                "be50_reason": be_reason,
                "be50_gross_pct": be["be50_gross_pct"],
                "be50_net_pct": be_net,
                "pnl_delta_pct": (be_net - base_net) if be_net is not None else None,
                "outcome_changed": change not in ("UNCHANGED_TP", "UNCHANGED_SL"),
                "change_class": change,
                "ambiguity_flag": be["ambiguity_flag"],
                "baseline_would_hit_tp_later": later_tp,
                "tp_group": tp_group(base_net),
                "first_signal_tf": str(tr["first_signal_tf"]),
                "highest_tf_reached": str(tr["highest_tf_reached"]),
            }
        )

    cmp = pd.DataFrame(rows)

    # equity paths
    base_df = cmp.copy()
    base_df["eq_reason"] = base_df["baseline_reason"]
    base_df["eq_net"] = base_df["baseline_net_pct"]
    be_df = cmp.copy()
    be_df["eq_reason"] = be_df["be50_reason"]
    be_df["eq_net"] = be_df["be50_net_pct"]

    base_eq, base_sum = simulate_equity_path(base_df, "eq_net", "eq_reason")
    be_eq, be_sum = simulate_equity_path(be_df, "eq_net", "eq_reason")

    counts = {
        "SL_TO_BE": int((cmp["change_class"] == "SL_TO_BE").sum()),
        "TP_TO_BE": int((cmp["change_class"] == "TP_TO_BE").sum()),
        "UNCHANGED_TP": int((cmp["change_class"] == "UNCHANGED_TP").sum()),
        "UNCHANGED_SL": int((cmp["change_class"] == "UNCHANGED_SL").sum()),
        "n_ambiguous": int((cmp["ambiguity_flag"] == "AMBIGUOUS_INTRABAR").sum()),
    }

    # saved loss / lost winner (sum of net pct deltas on changed trades)
    sl_to_be = cmp[cmp["change_class"] == "SL_TO_BE"]
    tp_to_be = cmp[cmp["change_class"] == "TP_TO_BE"]
    total_saved_loss = float((-sl_to_be["baseline_net_pct"] + sl_to_be["be50_net_pct"]).sum()) if len(sl_to_be) else 0.0
    # saved = improvement on those trades (baseline was negative)
    total_lost_winner = float((tp_to_be["baseline_net_pct"] - tp_to_be["be50_net_pct"]).sum()) if len(tp_to_be) else 0.0
    net_benefit_pct = total_saved_loss - total_lost_winner
    # equity-space benefit
    equity_delta = be_sum["end_total"] - base_sum["end_total"]

    # prior loser audit: SLs with mfe>=50% — approximate using change or recompute
    # Use: baseline SL where be50_triggered True OR SL_TO_BE
    baseline_sl = cmp[cmp["baseline_reason"] == "SL"]
    sl_mfe50_saved = int((baseline_sl["change_class"] == "SL_TO_BE").sum())
    sl_triggered_not_be = int(
        ((baseline_sl["be50_triggered"]) & (baseline_sl["be50_reason"] != "BE")).sum()
    )

    # cluster 9-13
    cluster = cmp[(cmp["july_n"] >= 9) & (cmp["july_n"] <= 13)].copy()

    # group comparisons
    group_rows = []
    for col, label in (("symbol", "symbol"), ("side", "side"), ("tp_group", "tp_group")):
        for gval, g in cmp.groupby(col):
            group_rows.append(
                {
                    "group_type": label,
                    "group": gval,
                    "trades": int(len(g)),
                    "baseline_net_sum": float(g["baseline_net_pct"].sum()),
                    "be50_net_sum": float(g["be50_net_pct"].sum()),
                    "delta_net_sum": float(g["be50_net_pct"].sum() - g["baseline_net_pct"].sum()),
                    "SL_TO_BE": int((g["change_class"] == "SL_TO_BE").sum()),
                    "TP_TO_BE": int((g["change_class"] == "TP_TO_BE").sum()),
                }
            )

    decision = decide(base_sum, be_sum, counts)

    payload = {
        "audit_version": AUDIT_VERSION,
        "price_resolution": "1m MySQL market_candles (no 1s/tick)",
        "fee_pct": FEE_PCT,
        "n_trades": int(len(cmp)),
        "comparison": cmp,
        "base_equity": base_eq,
        "be_equity": be_eq,
        "base_summary": base_sum,
        "be_summary": be_sum,
        "counts": counts,
        "total_saved_loss_pct": total_saved_loss,
        "total_lost_winner_profit_pct": total_lost_winner,
        "be50_net_benefit_pct": net_benefit_pct,
        "equity_delta": equity_delta,
        "sl_to_be_df": sl_to_be,
        "tp_to_be_df": tp_to_be,
        "cluster_9_13": cluster,
        "groups": pd.DataFrame(group_rows),
        "decision": decision,
        "sl_baseline_n": int(len(baseline_sl)),
        "sl_to_be_n": sl_mfe50_saved,
        "out_dir": out_dir,
    }
    return payload
