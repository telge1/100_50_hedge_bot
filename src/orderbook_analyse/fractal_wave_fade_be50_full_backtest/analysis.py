"""Orchestrate full-history BE50 vs baseline A/B."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE
from orderbook_analyse.fractal_wave_fade_be50_full_backtest import (
    AUDIT_VERSION,
    CASHOUT_RATE,
    COVERAGE_RATE,
    EXPECTED_N,
    FEE_PCT,
    OUT_DIR_DEFAULT,
    REF_TRADES,
    START_ACTIVE,
    START_RESERVE,
)
from orderbook_analyse.fractal_wave_fade_be50_full_backtest.equity import simulate_equity_path
from orderbook_analyse.fractal_wave_fade_be50_full_backtest.simulate_fast import (
    prepare_book,
    simulate_be50_trade_fast,
    trade_levels,
)
from orderbook_analyse.fractal_wave_fade_be50_full_backtest.streaks import (
    detail_top_streaks,
    distribution_frame,
    streak_summary,
)
from orderbook_analyse.fractal_wave_fade_be50_july_2026.analysis import (
    classify_change,
    tp_group,
)
from orderbook_analyse.fractal_wave_fade_cashout_reimbursement_analysis.simulate import (
    simulate as cashout_simulate,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def load_trades() -> pd.DataFrame:
    t = pd.read_csv(REF_TRADES)
    for c in ("entry_time", "exit_time", "signal_time"):
        t[c] = pd.to_datetime(t[c], utc=True)
    t = t.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    t["seq"] = np.arange(len(t))
    return t


def reproduce_baseline(trades: pd.DataFrame) -> dict[str, Any]:
    """Must match cashout 30%/100% full backtest reference numbers."""
    if len(trades) != EXPECTED_N:
        return {
            "ok": False,
            "reason": f"trade_count={len(trades)} expected={EXPECTED_N}",
        }
    ref = cashout_simulate(
        trades,
        cashout_rate=CASHOUT_RATE,
        coverage_rate=COVERAGE_RATE,
        start_active=START_ACTIVE,
        start_reserve=START_RESERVE,
    )["summary"]
    # Local equity path in fixed entry order (July-consistent for A/B)
    base_df = trades.copy()
    base_df["eq_net"] = base_df["net_return_pct"]
    base_df["eq_reason"] = base_df["exit_reason"]
    path, local = simulate_equity_path(base_df, "eq_net", "eq_reason", exit_time_col="exit_time")

    # Cashout sim sorts by exit_time; check wealth parity with local if order matches
    end_ref = float(ref["end_total_wealth"])
    end_local = float(local["end_total"])
    dd_ref = float(ref["total_max_dd_pct"])
    # Reference from prior analysis: ~7.758e9, DD ~-22.04%
    ok_ref = abs(end_ref / 7.758e9 - 1.0) < 0.002 and abs(dd_ref - (-22.04)) < 0.05
    # Local vs cashout: if entry order != exit order, ends may differ; report both
    order_same = list(trades.sort_values(["exit_time", "trade_id"])["trade_id"]) == list(
        trades["trade_id"]
    )
    ok_local = abs(end_local / end_ref - 1.0) < 1e-9 if order_same else True
    ok = ok_ref and ok_local and abs(end_local - end_ref) / max(end_ref, 1) < 0.01
    # Prefer cashout-sorted baseline for reproduction gate; A/B uses fixed entry order.
    # If orders differ, reproduction uses cashout numbers; A/B still uses entry-order path.
    return {
        "ok": bool(ok_ref),
        "order_same": order_same,
        "cashout_summary": ref,
        "local_summary": local,
        "local_path": path,
        "end_total_cashout": end_ref,
        "end_total_local": end_local,
        "max_dd_cashout": dd_ref,
        "max_dd_local": float(local["max_dd_pct"]),
        "n_trades": int(len(trades)),
        "symbols": sorted(trades["symbol"].unique().tolist()),
        "period_start": str(trades["entry_time"].min()),
        "period_end": str(trades["exit_time"].max()),
        "reason": None if ok_ref else "cashout_30_100_reference_mismatch",
    }


def load_1m_books(symbols, start, end) -> dict[str, Any]:
    load_env_file(ENV_FILE)
    out = {}
    for sym in symbols:
        print(f"[1m] {sym} …", flush=True)
        c = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        ts = pd.to_datetime(c["timestamp"], utc=True)
        c = c.loc[(ts >= start - pd.Timedelta(days=1)) & (ts <= end + pd.Timedelta(days=12))].reset_index(
            drop=True
        )
        out[sym] = prepare_book(c)
        print(f"[1m] {sym}: n={len(c)}", flush=True)
    return out


def worst_k_trade_block(nets: np.ndarray, k: int = 10) -> float:
    if len(nets) < k:
        return float(nets.sum()) if len(nets) else 0.0
    cs = np.concatenate([[0.0], np.cumsum(nets)])
    # min of cs[i+k]-cs[i]
    return float(min(cs[i + k] - cs[i] for i in range(len(nets) - k + 1)))


def month_local_stats(cmp: pd.DataFrame) -> pd.DataFrame:
    rows = []
    cmp = cmp.copy()
    cmp["month"] = pd.to_datetime(cmp["entry_time"], utc=True).dt.strftime("%Y-%m")
    for month, g in cmp.groupby("month", sort=True):
        g = g.reset_index(drop=True)
        b = g.copy()
        b["eq_net"] = b["baseline_net_pct"]
        b["eq_reason"] = b["baseline_reason"]
        e = g.copy()
        e["eq_net"] = e["be50_net_pct"]
        e["eq_reason"] = e["be50_reason"]
        _, bs = simulate_equity_path(b, "eq_net", "eq_reason")
        _, es = simulate_equity_path(e, "eq_net", "eq_reason")
        b_sl = streak_summary(list(g["baseline_reason"]), count_as={"SL"}, label="b")
        e_sl = streak_summary(list(g["be50_reason"]), count_as={"SL"}, label="e")
        rows.append(
            {
                "month": month,
                "trades": int(len(g)),
                "baseline_pct": bs["performance_pct"],
                "be50_pct": es["performance_pct"],
                "baseline_max_dd": bs["max_dd_pct"],
                "be50_max_dd": es["max_dd_pct"],
                "longest_sl_baseline": b_sl["max_streak"],
                "longest_sl_be50": e_sl["max_streak"],
                "be50_better_perf": es["performance_pct"] > bs["performance_pct"],
                "be50_better_dd": es["max_dd_pct"] > bs["max_dd_pct"],  # less negative
                "be50_shorter_sl": e_sl["max_streak"] < b_sl["max_streak"],
            }
        )
    return pd.DataFrame(rows)


def cluster_dd(nets: list[float]) -> float:
    """Peak-to-trough on cumulative additive nets within cluster (proxy)."""
    if not nets:
        return 0.0
    eq = np.cumsum([0.0] + list(nets))
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return float(dd.min())


def decide(payload: dict[str, Any]) -> str:
    b, e = payload["base_summary"], payload["be_summary"]
    c = payload["counts"]
    true_b = payload["true_sl_base"]
    true_e = payload["true_sl_be"]
    monthly = payload["monthly"]
    n_amb = c["n_ambiguous"]
    if n_amb >= max(50, int(0.02 * payload["n_trades"])):
        return "BE50_FULL_BACKTEST_AMBIGUOUS"

    end_ratio = e["end_total"] / b["end_total"] if b["end_total"] > 0 else 1.0
    d_dd = e["max_dd_pct"] - b["max_dd_pct"]  # positive => better DD
    streak_cut = true_b["max_streak"] - true_e["max_streak"]
    ge3_cut = true_b["n_ge_3"] - true_e["n_ge_3"]
    ge5_cut = true_b["n_ge_5"] - true_e["n_ge_5"]
    ret_dd_b = abs(b["performance_pct"] / b["max_dd_pct"]) if b["max_dd_pct"] else None
    ret_dd_e = abs(e["performance_pct"] / e["max_dd_pct"]) if e["max_dd_pct"] else None
    ret_dd_improve = (
        ret_dd_e is not None and ret_dd_b is not None and ret_dd_e >= ret_dd_b
    )
    months_better = int(monthly["be50_better_perf"].sum()) if len(monthly) else 0
    months_dd = int(monthly["be50_better_dd"].sum()) if len(monthly) else 0
    months_sl = int(monthly["be50_shorter_sl"].sum()) if len(monthly) else 0
    n_months = max(len(monthly), 1)
    streak_benefit = streak_cut >= 2 and ge3_cut >= 10 and ge5_cut >= 5
    dd_benefit = d_dd >= 3.0 and months_dd >= n_months * 0.7
    monthly_return_mixed = abs(months_better / n_months - 0.5) <= 0.15

    if streak_cut <= 0 and ge3_cut <= 0 and ge5_cut <= 0:
        return "BE50_FULL_BACKTEST_NO_STREAK_BENEFIT"

    # Severe compounding haircut without commensurate risk win
    if end_ratio < 0.55 and not (streak_benefit and dd_benefit):
        return "BE50_FULL_BACKTEST_HURTS_RETURNS_TOO_MUCH"
    if end_ratio < 0.75 and c["TP_TO_BE"] > c["SL_TO_BE"] * 1.15 and d_dd < 4.0:
        return "BE50_FULL_BACKTEST_HURTS_RETURNS_TOO_MUCH"

    strong = (
        end_ratio >= 0.95
        and d_dd >= 2.0
        and streak_cut >= 2
        and ge5_cut >= 1
        and months_better >= n_months * 0.55
    )
    if strong:
        return "BE50_FULL_BACKTEST_STRONGLY_BETTER"

    # Core case: clear SL-streak + DD improvement, return cost accepted on risk-adj basis
    risk_adj = (
        streak_benefit
        and dd_benefit
        and ret_dd_improve
        and months_sl >= n_months * 0.6
        and end_ratio >= 0.60
        and c["SL_TO_BE"] >= c["TP_TO_BE"] * 0.9
    )
    if risk_adj:
        return "BE50_FULL_BACKTEST_BETTER_RISK_ADJUSTED"

    if end_ratio < 0.80 and not streak_benefit:
        return "BE50_FULL_BACKTEST_HURTS_RETURNS_TOO_MUCH"

    if abs(end_ratio - 1.0) < 0.05 and abs(d_dd) < 1.0 and streak_cut <= 1 and monthly_return_mixed:
        return "BE50_FULL_BACKTEST_NEUTRAL"

    if streak_cut >= 1 and d_dd > 0 and end_ratio >= 0.90:
        return "BE50_FULL_BACKTEST_BETTER_RISK_ADJUSTED"

    if end_ratio < 0.85 and c["TP_TO_BE"] >= c["SL_TO_BE"] and d_dd < 5.0:
        return "BE50_FULL_BACKTEST_HURTS_RETURNS_TOO_MUCH"

    return "BE50_FULL_BACKTEST_NEUTRAL"


def run_analysis(*, out_dir: Path = OUT_DIR_DEFAULT) -> dict[str, Any]:
    trades = load_trades()
    print(f"[baseline] reproducing n={len(trades)} …", flush=True)
    base_rep = reproduce_baseline(trades)
    if not base_rep["ok"]:
        return {
            "baseline_reproduction_failed": True,
            "baseline_reproduction": base_rep,
            "decision": "BASELINE_REPRODUCTION_FAILED",
            "out_dir": out_dir,
        }

    books = load_1m_books(
        sorted(trades["symbol"].unique()),
        trades["entry_time"].min(),
        trades["exit_time"].max(),
    )

    rows = []
    n = len(trades)
    print(f"[sim] BE50 on {n} trades …", flush=True)
    for k, (_, tr) in enumerate(trades.iterrows(), start=1):
        if k % 500 == 0 or k == 1:
            print(f"  … {k}/{n}", flush=True)
        lev = trade_levels(tr)
        be = simulate_be50_trade_fast(tr, books[str(tr["symbol"])], lev)
        base_net = float(tr["net_return_pct"])
        base_reason = str(tr["exit_reason"])
        be_net = be["be50_net_pct"]
        be_reason = be["be50_reason"]
        if be_net is None:
            be_net = base_net
            be_reason = "DATA_MISSING"
        change = classify_change(base_reason, be_reason, base_net, be_net)
        rows.append(
            {
                "seq": int(tr["seq"]),
                "trade_id": int(tr["trade_id"]),
                "entry_time": tr["entry_time"],
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
                "exit_time_baseline": tr["exit_time"],
                "be50_triggered": be["be50_triggered"],
                "be50_trigger_time": be["be50_trigger_time"],
                "be50_exit_time": be["be50_exit_time"],
                "be50_exit_price": be["be50_exit_price"],
                "be50_reason": be_reason,
                "be50_gross_pct": be["be50_gross_pct"],
                "be50_net_pct": float(be_net),
                "pnl_delta_pct": float(be_net) - base_net,
                "outcome_changed": change
                not in ("UNCHANGED_TP", "UNCHANGED_SL", "UNCHANGED_TIMEOUT", "UNCHANGED_HIGHER_TF_CONFLICT"),
                "change_class": change,
                "ambiguity_flag": be["ambiguity_flag"],
                "tp_group": tp_group(base_net) if base_reason in ("TP", "SL") else f"OTHER/{base_reason}",
                "first_signal_tf": str(tr["first_signal_tf"]),
                "highest_tf_reached": str(tr["highest_tf_reached"]),
            }
        )

    cmp = pd.DataFrame(rows)

    base_df = cmp.copy()
    base_df["eq_net"] = base_df["baseline_net_pct"]
    base_df["eq_reason"] = base_df["baseline_reason"]
    be_df = cmp.copy()
    be_df["eq_net"] = be_df["be50_net_pct"]
    be_df["eq_reason"] = be_df["be50_reason"]

    base_eq, base_sum = simulate_equity_path(
        base_df, "eq_net", "eq_reason", exit_time_col="exit_time_baseline"
    )
    be_eq, be_sum = simulate_equity_path(
        be_df, "eq_net", "eq_reason", exit_time_col="be50_exit_time"
    )

    # Align baseline equity end with cashout reference (entry-order path)
    # Document if cashout-sorted differs
    counts = {
        "SL_TO_BE": int((cmp["change_class"] == "SL_TO_BE").sum()),
        "TP_TO_BE": int((cmp["change_class"] == "TP_TO_BE").sum()),
        "UNCHANGED_TP": int((cmp["change_class"] == "UNCHANGED_TP").sum()),
        "UNCHANGED_SL": int((cmp["change_class"] == "UNCHANGED_SL").sum()),
        "n_ambiguous": int((cmp["ambiguity_flag"] == "AMBIGUOUS_INTRABAR").sum()),
    }

    sl_to_be = cmp[cmp["change_class"] == "SL_TO_BE"]
    tp_to_be = cmp[cmp["change_class"] == "TP_TO_BE"]
    total_saved_loss = (
        float((-sl_to_be["baseline_net_pct"] + sl_to_be["be50_net_pct"]).sum()) if len(sl_to_be) else 0.0
    )
    total_lost_winner = (
        float((tp_to_be["baseline_net_pct"] - tp_to_be["be50_net_pct"]).sum()) if len(tp_to_be) else 0.0
    )
    net_benefit_pct = total_saved_loss - total_lost_winner
    equity_delta = be_sum["end_total"] - base_sum["end_total"]

    base_reasons = list(cmp["baseline_reason"])
    be_reasons = list(cmp["be50_reason"])
    true_sl_base = streak_summary(base_reasons, count_as={"SL"}, label="TRUE_SL_BASE")
    true_sl_be = streak_summary(be_reasons, count_as={"SL"}, label="TRUE_SL_BE50")
    nw_base = streak_summary(base_reasons, count_as={"SL", "BE"}, label="NON_WINNER_BASE")
    nw_be = streak_summary(be_reasons, count_as={"SL", "BE"}, label="NON_WINNER_BE50")

    top_detail = detail_top_streaks(cmp, base_reasons, be_reasons, true_sl_base["top_streaks"])
    # cluster DD on additive nets
    if len(top_detail):
        dd_b, dd_e, rem = [], [], []
        for _, r in top_detail.iterrows():
            sub = cmp.iloc[int(r["start_i"]) : int(r["end_i"]) + 1]
            dd_b.append(cluster_dd(list(sub["baseline_net_pct"])))
            dd_e.append(cluster_dd(list(sub["be50_net_pct"])))
            rem.append(float(sub["be50_net_pct"].sum()))
        top_detail["cluster_dd_baseline_add"] = dd_b
        top_detail["cluster_dd_be50_add"] = dd_e
        top_detail["remaining_cum_net_be50"] = rem

    # equity-path cluster DD using total equity within window
    if len(top_detail):
        eq_dd_b, eq_dd_e = [], []
        for _, r in top_detail.iterrows():
            a, b_ = int(r["start_i"]), int(r["end_i"])
            eb = base_eq.iloc[a : b_ + 1]["total_after"].astype(float).to_numpy()
            ee = be_eq.iloc[a : b_ + 1]["total_after"].astype(float).to_numpy()
            # include equity just before first trade of cluster
            if a > 0:
                eb = np.concatenate([[float(base_eq.iloc[a - 1]["total_after"])], eb])
                ee = np.concatenate([[float(be_eq.iloc[a - 1]["total_after"])], ee])
            else:
                eb = np.concatenate([[START_ACTIVE], eb])
                ee = np.concatenate([[START_ACTIVE], ee])
            peak_b = np.maximum.accumulate(eb)
            peak_e = np.maximum.accumulate(ee)
            eq_dd_b.append(float((((eb / peak_b) - 1.0) * 100.0).min()))
            eq_dd_e.append(float((((ee / peak_e) - 1.0) * 100.0).min()))
        top_detail["drawdown_cluster_baseline_pct"] = eq_dd_b
        top_detail["drawdown_cluster_be50_pct"] = eq_dd_e

    monthly = month_local_stats(cmp)

    # symbol / side / tp profile
    def group_table(col: str) -> pd.DataFrame:
        rows_g = []
        for gval, g in cmp.groupby(col):
            b_sl = streak_summary(list(g["baseline_reason"]), count_as={"SL"}, label="b")
            e_sl = streak_summary(list(g["be50_reason"]), count_as={"SL"}, label="e")
            rows_g.append(
                {
                    col: gval,
                    "trades": int(len(g)),
                    "base_net": float(g["baseline_net_pct"].sum()),
                    "be50_net": float(g["be50_net_pct"].sum()),
                    "base_max_sl_streak": b_sl["max_streak"],
                    "be50_max_sl_streak": e_sl["max_streak"],
                    "SL_TO_BE": int((g["change_class"] == "SL_TO_BE").sum()),
                    "TP_TO_BE": int((g["change_class"] == "TP_TO_BE").sum()),
                    "base_pnl": float(g["baseline_net_pct"].sum()),
                    "be50_pnl": float(g["be50_net_pct"].sum()),
                }
            )
        return pd.DataFrame(rows_g)

    symbol_cmp = group_table("symbol")
    side_cmp = group_table("side")
    tp_cmp = group_table("tp_group")

    # risk/return
    def risk_block(summary, nets):
        ret = summary["performance_pct"]
        mdd = summary["max_dd_pct"]
        avg_dd = summary["avg_dd_pct"]
        return {
            "return_over_max_dd": abs(ret / mdd) if mdd else None,
            "return_over_avg_dd": abs(ret / avg_dd) if avg_dd else None,
            "profit_factor": summary["profit_factor"],
            "expectancy_pct": float(np.mean(nets)),
            "worst_10_trade_block_pct": worst_k_trade_block(np.asarray(nets, dtype=float), 10),
        }

    risk_base = risk_block(base_sum, cmp["baseline_net_pct"].to_numpy())
    risk_be = risk_block(be_sum, cmp["be50_net_pct"].to_numpy())
    if len(monthly):
        risk_base["worst_month_pct"] = float(monthly["baseline_pct"].min())
        risk_be["worst_month_pct"] = float(monthly["be50_pct"].min())
    else:
        risk_base["worst_month_pct"] = None
        risk_be["worst_month_pct"] = None
    risk_base["worst_true_sl_streak"] = true_sl_base["max_streak"]
    risk_be["worst_true_sl_streak"] = true_sl_be["max_streak"]
    # worst SL streak in additive %
    def worst_streak_sum(reasons, nets, count_as):
        best = 0.0
        i = 0
        n_ = len(reasons)
        while i < n_:
            if reasons[i] not in count_as:
                i += 1
                continue
            j = i
            s = 0.0
            while j < n_ and reasons[j] in count_as:
                s += nets[j]
                j += 1
            best = min(best, s)
            i = j
        return best

    risk_base["worst_sl_streak_net_pct"] = worst_streak_sum(
        base_reasons, cmp["baseline_net_pct"].to_numpy(), {"SL"}
    )
    risk_be["worst_sl_streak_net_pct"] = worst_streak_sum(
        be_reasons, cmp["be50_net_pct"].to_numpy(), {"SL"}
    )

    # equity comparison frame
    eq_cmp = pd.DataFrame(
        {
            "seq": base_eq["seq"],
            "trade_id": base_eq["trade_id"],
            "baseline_total": base_eq["total_after"],
            "be50_total": be_eq["total_after"],
            "baseline_dd": base_eq["drawdown_pct"],
            "be50_dd": be_eq["drawdown_pct"],
            "baseline_reason": base_eq["reason"],
            "be50_reason": be_eq["reason"],
            "baseline_net": base_eq["net_return_pct"],
            "be50_net": be_eq["net_return_pct"],
        }
    )

    payload = {
        "baseline_reproduction_failed": False,
        "baseline_reproduction": base_rep,
        "audit_version": AUDIT_VERSION,
        "price_resolution": "1m MySQL market_candles (no 1s/tick)",
        "fee_pct": FEE_PCT,
        "n_trades": int(len(cmp)),
        "comparison": cmp,
        "base_equity": base_eq,
        "be_equity": be_eq,
        "equity_comparison": eq_cmp,
        "base_summary": base_sum,
        "be_summary": be_sum,
        "counts": counts,
        "total_saved_loss_pct": total_saved_loss,
        "total_lost_winner_profit_pct": total_lost_winner,
        "be50_net_benefit_pct": net_benefit_pct,
        "equity_delta": equity_delta,
        "sl_to_be_df": sl_to_be,
        "tp_to_be_df": tp_to_be,
        "changed_trades": cmp[cmp["outcome_changed"]].copy(),
        "true_sl_base": true_sl_base,
        "true_sl_be": true_sl_be,
        "nw_base": nw_base,
        "nw_be": nw_be,
        "sl_streak_distribution": distribution_frame(
            true_sl_base["distribution"], true_sl_be["distribution"]
        ),
        "non_winner_streak_distribution": distribution_frame(
            nw_base["distribution"], nw_be["distribution"]
        ),
        "top_sl_streaks": top_detail,
        "monthly": monthly,
        "symbol_comparison": symbol_cmp,
        "side_comparison": side_cmp,
        "tp_profile_comparison": tp_cmp,
        "risk_base": risk_base,
        "risk_be": risk_be,
        "out_dir": out_dir,
    }
    payload["decision"] = decide(payload)
    return payload
