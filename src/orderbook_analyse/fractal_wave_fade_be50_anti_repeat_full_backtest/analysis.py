"""Orchestrate Baseline / BE50 / BE50+AntiRepeat comparison."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_be50_anti_repeat_full_backtest import (
    AUDIT_VERSION,
    BE50_DIR,
    DEFINITIONS_DOC,
    FROZEN_DIR,
    GLOBAL_DIR,
    LARGE_DD_WINDOWS,
    OUT_DIR_DEFAULT,
    REF_BE50_END,
    REF_BE50_GE10_DD,
    REF_BE50_MAX_DD,
)
from orderbook_analyse.fractal_wave_fade_be50_anti_repeat_full_backtest.anti_repeat import (
    apply_anti_repeat,
    build_wave_reset_index,
)
from orderbook_analyse.fractal_wave_fade_be50_drawdown_audit.episodes import extract_episodes
from orderbook_analyse.fractal_wave_fade_be50_full_backtest.equity import simulate_equity_path
from orderbook_analyse.fractal_wave_fade_be50_full_backtest.streaks import streak_summary
from orderbook_analyse.fractal_wave_fade_be50_july_2026.equity import (
    simulate_equity_path as july_equity,
)


def _utc_series(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True)


def load_inputs() -> dict[str, pd.DataFrame]:
    be50 = pd.read_csv(BE50_DIR / "full_trade_comparison.csv")
    for c in ("entry_time", "exit_time_baseline", "be50_exit_time", "be50_trigger_time"):
        if c in be50.columns:
            be50[c] = _utc_series(be50[c])
    base_trades = pd.read_csv(GLOBAL_DIR / "trades.csv")
    for c in ("entry_time", "exit_time", "signal_time"):
        base_trades[c] = _utc_series(base_trades[c])
    suppressed = pd.read_csv(GLOBAL_DIR / "suppressed_signals.csv")
    for c in ("signal_available_at", "entry_available_at"):
        suppressed[c] = _utc_series(suppressed[c])
    return {"be50": be50, "baseline": base_trades, "suppressed": suppressed}


def _metrics_from_path(path: pd.DataFrame, summary: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    true = streak_summary(reasons, count_as={"SL"}, label="TRUE_SL")
    nw = streak_summary(reasons, count_as={"SL", "BE"}, label="NON_WINNER")
    eq = path["total_after"].astype(float).to_numpy()
    eps = extract_episodes(
        eq,
        times=list(path["exit_time"]) if "exit_time" in path.columns else None,
        reasons=reasons,
        trade_ids=list(path["trade_id"].astype(int)) if "trade_id" in path.columns else None,
        start_equity=1000.0,
    )
    depths = np.sort(eps["max_drawdown_pct"].to_numpy()) if len(eps) else np.array([])
    return {
        "summary": summary,
        "true_sl": true,
        "non_winner": nw,
        "episodes": eps,
        "n_ge_5_dd": int((eps["max_drawdown_pct"] <= -5).sum()) if len(eps) else 0,
        "n_ge_10_dd": int((eps["max_drawdown_pct"] <= -10).sum()) if len(eps) else 0,
        "n_ge_12_dd": int((eps["max_drawdown_pct"] <= -12).sum()) if len(eps) else 0,
        "n_ge_14_dd": int((eps["max_drawdown_pct"] <= -14).sum()) if len(eps) else 0,
        "max_dd": float(depths[0]) if len(depths) else summary.get("max_dd_pct"),
        "second_dd": float(depths[1]) if len(depths) > 1 else None,
        "third_dd": float(depths[2]) if len(depths) > 2 else None,
        "median_recovery_hours": float(eps["duration_trough_to_recovery_hours"].median())
        if len(eps) and eps["duration_trough_to_recovery_hours"].notna().any()
        else None,
    }


def simulate_variant(trades: pd.DataFrame, net_col: str, reason_col: str, exit_col: str) -> dict[str, Any]:
    df = trades.copy().reset_index(drop=True)
    df["eq_net"] = df[net_col].astype(float)
    df["eq_reason"] = df[reason_col].astype(str)
    path, summary = simulate_equity_path(df, "eq_net", "eq_reason", exit_time_col=exit_col)
    # ensure exit_time on path
    if "exit_time" not in path.columns or path["exit_time"].isna().all():
        path["exit_time"] = df[exit_col].values
    return _metrics_from_path(path, summary, list(df["eq_reason"]),)


def hour_bucket(h: float) -> str:
    if h < 1:
        return "<1h"
    if h < 3:
        return "1-3h"
    if h < 6:
        return "3-6h"
    if h < 12:
        return "6-12h"
    if h < 24:
        return "12-24h"
    return ">24h"


def window_dd(path: pd.DataFrame, peak: str, recovery: str) -> float | None:
    if path.empty or "exit_time" not in path.columns:
        return None
    t = pd.to_datetime(path["exit_time"], utc=True)
    p0 = pd.Timestamp(peak, tz="UTC")
    p1 = pd.Timestamp(recovery, tz="UTC") + pd.Timedelta(days=1)
    # include equity just before window
    mask = (t >= p0) & (t <= p1)
    if not mask.any():
        return None
    idxs = np.flatnonzero(mask.to_numpy())
    start = max(int(idxs[0]) - 1, 0)
    end = int(idxs[-1])
    eq = path["total_after"].astype(float).to_numpy()[start : end + 1]
    if start == 0:
        eq = np.concatenate([[1000.0], eq])
    else:
        eq = np.concatenate([[float(path.iloc[start - 1]["total_after"])], eq])
    peak_e = np.maximum.accumulate(eq)
    dd = (eq / peak_e - 1.0) * 100.0
    return float(dd.min())


def decide(payload: dict[str, Any]) -> str:
    b = payload["be50_m"]
    a = payload["anti_m"]
    blocked = payload["blocked"]
    if payload.get("reset_not_identifiable"):
        return "ANTI_REPEAT_RESET_NOT_RELIABLY_IDENTIFIABLE"

    n_block = int(len(blocked))
    if n_block < 5:
        return "ANTI_REPEAT_NO_MEANINGFUL_EFFECT"

    sl_av = int((blocked["original_be50_outcome"] == "SL").sum())
    tp_lost = int((blocked["original_be50_outcome"] == "TP").sum())
    be_av = int((blocked["original_be50_outcome"] == "BE").sum())

    d_dd = a["max_dd"] - b["max_dd"]  # positive better
    d_ge10 = b["n_ge_10_dd"] - a["n_ge_10_dd"]
    d_streak = b["true_sl"]["max_streak"] - a["true_sl"]["max_streak"]
    d_ge3 = b["true_sl"]["n_ge_3"] - a["true_sl"]["n_ge_3"]
    d_ge5 = b["true_sl"]["n_ge_5"] - a["true_sl"]["n_ge_5"]
    end_ratio = a["summary"]["end_total"] / b["summary"]["end_total"] if b["summary"]["end_total"] else 1.0

    ret_dd_b = abs(b["summary"]["performance_pct"] / b["max_dd"]) if b["max_dd"] else None
    ret_dd_a = abs(a["summary"]["performance_pct"] / a["max_dd"]) if a["max_dd"] else None
    ret_dd_up = ret_dd_a is not None and ret_dd_b is not None and ret_dd_a >= ret_dd_b

    if tp_lost > sl_av * 1.5 and end_ratio < 0.95:
        return "ANTI_REPEAT_BLOCKS_TOO_MANY_WINNERS"

    strong = (
        d_dd >= 2.0
        and d_ge10 >= 2
        and (d_streak >= 1 or d_ge3 >= 5)
        and end_ratio >= 0.9
        and sl_av >= tp_lost
    )
    if strong:
        return "ANTI_REPEAT_STRONGLY_IMPROVES_RISK"

    improved = (
        (d_dd >= 0.5 or d_ge10 >= 1 or d_streak >= 1 or d_ge3 >= 3 or d_ge5 >= 1)
        and end_ratio >= 0.85
        and sl_av >= tp_lost * 0.8
        and (ret_dd_up or d_dd > 0)
    )
    if improved:
        return "ANTI_REPEAT_IMPROVES_RISK_ADJUSTED"

    if abs(d_dd) < 0.3 and d_ge10 == 0 and d_streak == 0 and abs(end_ratio - 1) < 0.02:
        return "ANTI_REPEAT_NEUTRAL"

    if tp_lost > sl_av and end_ratio < 1.0:
        return "ANTI_REPEAT_BLOCKS_TOO_MANY_WINNERS"

    return "ANTI_REPEAT_NEUTRAL"


def run_analysis(*, out_dir: Path = OUT_DIR_DEFAULT) -> dict[str, Any]:
    print(DEFINITIONS_DOC, flush=True)
    print(f"[freeze] expecting frozen dir {FROZEN_DIR} …", flush=True)
    if not FROZEN_DIR.exists():
        raise RuntimeError(f"Missing frozen baseline: {FROZEN_DIR}")

    inp = load_inputs()
    be50 = inp["be50"]
    baseline = inp["baseline"]
    suppressed = inp["suppressed"]

    # verify BE50 frozen numbers on full comparison equity
    be50_m_check = simulate_variant(be50, "be50_net_pct", "be50_reason", "be50_exit_time")
    if abs(be50_m_check["summary"]["end_total"] - REF_BE50_END) / REF_BE50_END > 0.002:
        print(
            f"[warn] BE50 end equity mismatch: {be50_m_check['summary']['end_total']} vs {REF_BE50_END}",
            flush=True,
        )
    if abs(be50_m_check["max_dd"] - REF_BE50_MAX_DD) > 0.02:
        print(
            f"[warn] BE50 max DD mismatch: {be50_m_check['max_dd']} vs {REF_BE50_MAX_DD}",
            flush=True,
        )

    print("[anti-repeat] building opposite-wave reset index (MySQL) …", flush=True)
    symbols = sorted(be50["symbol"].astype(str).unique())
    wave_index = build_wave_reset_index(symbols)
    if not wave_index:
        return {
            "decision": "ANTI_REPEAT_RESET_NOT_RELIABLY_IDENTIFIABLE",
            "reset_not_identifiable": True,
            "out_dir": out_dir,
            "audit_version": AUDIT_VERSION,
            "frozen_dir": str(FROZEN_DIR),
        }

    print("[anti-repeat] applying SAME_SIDE_BLOCK (opposite-wave reset) …", flush=True)
    ar = apply_anti_repeat(be50, wave_index)
    kept = ar["kept"]
    blocked = ar["blocked"]
    resets = ar["resets"]
    post = ar["post_sl_signals"]

    print(f"  kept={len(kept)} blocked={len(blocked)} resets={len(resets)}", flush=True)

    # Variant A: original baseline (no BE50)
    base_aligned = baseline.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    base_m = simulate_variant(base_aligned, "net_return_pct", "exit_reason", "exit_time")

    # Variant B: BE50 full
    be50_m = be50_m_check

    # Variant C: anti-repeat kept
    anti_m = simulate_variant(kept, "be50_net_pct", "be50_reason", "be50_exit_time")

    # blocked outcome summary
    block_summary = {
        "n_blocked": int(len(blocked)),
        "sl_avoided": int((blocked["original_be50_outcome"] == "SL").sum()) if len(blocked) else 0,
        "be_avoided": int((blocked["original_be50_outcome"] == "BE").sum()) if len(blocked) else 0,
        "tp_lost": int((blocked["original_be50_outcome"] == "TP").sum()) if len(blocked) else 0,
    }
    if len(blocked):
        sl_b = blocked[blocked["original_be50_outcome"] == "SL"]
        tp_b = blocked[blocked["original_be50_outcome"] == "TP"]
        be_b = blocked[blocked["original_be50_outcome"] == "BE"]
        # avoided loss ≈ -sum(negative be50 nets of SL blocked) i.e. sum of |nets| saved
        avoided_loss = float((-sl_b["original_be50_net_pct"]).sum()) if len(sl_b) else 0.0
        lost_profit = float(tp_b["original_be50_net_pct"].sum()) if len(tp_b) else 0.0
        # BE blocked: small fee losses avoided (negative nets ~ -0.11)
        avoided_be = float((-be_b["original_be50_net_pct"]).sum()) if len(be_b) else 0.0
    else:
        avoided_loss = lost_profit = avoided_be = 0.0
    block_summary.update(
        {
            "avoided_loss_pp": avoided_loss,
            "lost_profit_pp": lost_profit,
            "avoided_be_pp": avoided_be,
            "net_block_effect_pp": avoided_loss + avoided_be - lost_profit,
        }
    )

    # post-SL distance buckets
    if len(post):
        post = post.copy()
        post["hour_bucket"] = post["hours_since_sl"].map(hour_bucket)
        bucket = (
            post.groupby(["hour_bucket", "be50_reason"])
            .size()
            .reset_index(name="n")
            .pivot_table(index="hour_bucket", columns="be50_reason", values="n", fill_value=0)
            .reset_index()
        )
    else:
        bucket = pd.DataFrame()

    # large DD windows
    be50_path, _ = simulate_equity_path(
        be50.assign(eq_net=be50["be50_net_pct"], eq_reason=be50["be50_reason"]),
        "eq_net",
        "eq_reason",
        exit_time_col="be50_exit_time",
    )
    anti_path, _ = simulate_equity_path(
        kept.assign(eq_net=kept["be50_net_pct"], eq_reason=kept["be50_reason"]),
        "eq_net",
        "eq_reason",
        exit_time_col="be50_exit_time",
    )
    be50_path["exit_time"] = be50["be50_exit_time"].values
    anti_path["exit_time"] = kept["be50_exit_time"].values

    large_rows = []
    for w in LARGE_DD_WINDOWS:
        p0 = pd.Timestamp(w["peak"], tz="UTC")
        p1 = pd.Timestamp(w["recovery"], tz="UTC") + pd.Timedelta(days=1)
        bmask = (be50["entry_time"] >= p0) & (be50["entry_time"] <= p1)
        # blocked in window
        if len(blocked):
            blk_w = blocked[
                (pd.to_datetime(blocked["timestamp"], utc=True) >= p0)
                & (pd.to_datetime(blocked["timestamp"], utc=True) <= p1)
            ]
        else:
            blk_w = blocked
        large_rows.append(
            {
                "episode": w["episode"],
                "be50_dd_reported": w["be50_dd"],
                "be50_dd_recomputed": window_dd(be50_path, w["peak"], w["recovery"]),
                "anti_repeat_dd": window_dd(anti_path, w["peak"], w["recovery"]),
                "repeat_trades_blocked": int(len(blk_w)),
                "sl_avoided": int((blk_w["original_be50_outcome"] == "SL").sum()) if len(blk_w) else 0,
                "tp_lost": int((blk_w["original_be50_outcome"] == "TP").sum()) if len(blk_w) else 0,
                "be_avoided": int((blk_w["original_be50_outcome"] == "BE").sum()) if len(blk_w) else 0,
            }
        )
    large_df = pd.DataFrame(large_rows)

    # July
    jul_be = be50[(be50["entry_time"] >= "2026-07-01") & (be50["entry_time"] < "2026-08-01")].copy()
    jul_be = jul_be.sort_values(["entry_time", "trade_id"]).reset_index(drop=True)
    jul_be["july_n"] = np.arange(1, len(jul_be) + 1)
    jul_kept = kept[(kept["entry_time"] >= "2026-07-01") & (kept["entry_time"] < "2026-08-01")].copy()
    # local july equity
    jb = jul_be.copy()
    jb["july_n"] = jb["july_n"]
    jb["eq_net"] = jb["be50_net_pct"]
    jb["eq_reason"] = jb["be50_reason"]
    _, jul_be_sum = july_equity(jb, "eq_net", "eq_reason")
    jk = jul_kept.copy().reset_index(drop=True)
    jk["july_n"] = np.arange(1, len(jk) + 1)
    jk["eq_net"] = jk["be50_net_pct"]
    jk["eq_reason"] = jk["be50_reason"]
    _, jul_ar_sum = july_equity(jk, "eq_net", "eq_reason") if len(jk) else (
        pd.DataFrame(),
        {
            "end_total": 1000.0,
            "performance_pct": 0.0,
            "max_dd_pct": 0.0,
            "longest_sl_streak": 0,
            "n_tp": 0,
            "n_sl": 0,
            "n_be": 0,
        },
    )
    cluster = jul_be[(jul_be["july_n"] >= 9) & (jul_be["july_n"] <= 13)].copy()
    if len(blocked):
        blocked_ids = set(blocked["trade_id"].astype(int))
    else:
        blocked_ids = set()
    cluster["blocked_by_anti_repeat"] = cluster["trade_id"].astype(int).isin(blocked_ids)

    # monthly comparison (local restart)
    def month_stats(df: pd.DataFrame, net_col: str, reason_col: str) -> pd.DataFrame:
        rows = []
        d = df.copy()
        d["month"] = pd.to_datetime(d["entry_time"], utc=True).dt.strftime("%Y-%m")
        for month, g in d.groupby("month", sort=True):
            g = g.reset_index(drop=True)
            g2 = g.copy()
            g2["eq_net"] = g2[net_col]
            g2["eq_reason"] = g2[reason_col]
            g2["july_n"] = np.arange(1, len(g2) + 1)
            _, s = july_equity(g2, "eq_net", "eq_reason")
            true = streak_summary(list(g2["eq_reason"]), count_as={"SL"}, label="t")
            rows.append(
                {
                    "month": month,
                    "trades": int(len(g)),
                    "perf_pct": s["performance_pct"],
                    "max_dd": s["max_dd_pct"],
                    "longest_sl": true["max_streak"],
                }
            )
        return pd.DataFrame(rows)

    monthly_be = month_stats(be50, "be50_net_pct", "be50_reason").rename(
        columns=lambda c: c if c == "month" else f"be50_{c}"
    )
    monthly_ar = month_stats(kept, "be50_net_pct", "be50_reason").rename(
        columns=lambda c: c if c == "month" else f"anti_{c}"
    )
    monthly = monthly_be.merge(monthly_ar, on="month", how="outer")

    # equity comparison frame
    # align by seq of kept vs full be50 is awkward; export separate columns via trade_id merge
    eq_cmp = be50_path[["trade_id", "total_after", "drawdown_pct", "reason", "net_return_pct"]].rename(
        columns={
            "total_after": "be50_total",
            "drawdown_pct": "be50_dd",
            "reason": "be50_reason",
            "net_return_pct": "be50_net",
        }
    )
    ar_eq = anti_path[["trade_id", "total_after", "drawdown_pct", "reason", "net_return_pct"]].rename(
        columns={
            "total_after": "anti_total",
            "drawdown_pct": "anti_dd",
            "reason": "anti_reason",
            "net_return_pct": "anti_net",
        }
    )
    equity_comparison = eq_cmp.merge(ar_eq, on="trade_id", how="outer").sort_values("trade_id")

    # streak comparison table
    def streak_row(label, m):
        t = m["true_sl"]
        n = m["non_winner"]
        return {
            "variant": label,
            "true_max": t["max_streak"],
            "true_2nd": t["second_max"],
            "true_3rd": t["third_max"],
            "true_ge2": t["n_ge_2"],
            "true_ge3": t["n_ge_3"],
            "true_ge4": t["n_ge_4"],
            "true_ge5": t["n_ge_5"],
            "nw_max": n["max_streak"],
            "nw_ge3": n["n_ge_3"],
            "nw_ge5": n["n_ge_5"],
        }

    streak_cmp = pd.DataFrame(
        [
            streak_row("baseline", base_m),
            streak_row("be50", be50_m),
            streak_row("be50_anti_repeat", anti_m),
        ]
    )

    dd_cmp = pd.DataFrame(
        [
            {
                "variant": "baseline",
                "max_dd": base_m["max_dd"],
                "second": base_m["second_dd"],
                "third": base_m["third_dd"],
                "n_ge_5": base_m["n_ge_5_dd"],
                "n_ge_10": base_m["n_ge_10_dd"],
                "n_ge_12": base_m["n_ge_12_dd"],
                "n_ge_14": base_m["n_ge_14_dd"],
                "median_recovery_hours": base_m["median_recovery_hours"],
            },
            {
                "variant": "be50",
                "max_dd": be50_m["max_dd"],
                "second": be50_m["second_dd"],
                "third": be50_m["third_dd"],
                "n_ge_5": be50_m["n_ge_5_dd"],
                "n_ge_10": be50_m["n_ge_10_dd"],
                "n_ge_12": be50_m["n_ge_12_dd"],
                "n_ge_14": be50_m["n_ge_14_dd"],
                "median_recovery_hours": be50_m["median_recovery_hours"],
            },
            {
                "variant": "be50_anti_repeat",
                "max_dd": anti_m["max_dd"],
                "second": anti_m["second_dd"],
                "third": anti_m["third_dd"],
                "n_ge_5": anti_m["n_ge_5_dd"],
                "n_ge_10": anti_m["n_ge_10_dd"],
                "n_ge_12": anti_m["n_ge_12_dd"],
                "n_ge_14": anti_m["n_ge_14_dd"],
                "median_recovery_hours": anti_m["median_recovery_hours"],
            },
        ]
    )

    # trade comparison (kept flag)
    trade_cmp = be50.copy()
    trade_cmp["kept_under_anti_repeat"] = ~trade_cmp["trade_id"].astype(int).isin(blocked_ids)
    if len(blocked):
        trade_cmp = trade_cmp.merge(
            blocked[
                [
                    "trade_id",
                    "block_reason",
                    "prev_sl_trade_id",
                    "hours_since_prev_sl",
                    "reset_already_occurred",
                ]
            ],
            on="trade_id",
            how="left",
        )
    else:
        trade_cmp["block_reason"] = None
        trade_cmp["prev_sl_trade_id"] = None
        trade_cmp["hours_since_prev_sl"] = None
        trade_cmp["reset_already_occurred"] = None

    payload = {
        "audit_version": AUDIT_VERSION,
        "out_dir": out_dir,
        "frozen_dir": str(FROZEN_DIR),
        "base_m": base_m,
        "be50_m": be50_m,
        "anti_m": anti_m,
        "blocked": blocked,
        "resets": resets,
        "kept": kept,
        "post_sl_signals": post,
        "repeat_distance_buckets": bucket,
        "block_summary": block_summary,
        "large_dd": large_df,
        "july_be50": jul_be_sum,
        "july_anti": jul_ar_sum,
        "july_cluster_9_13": cluster,
        "monthly": monthly,
        "equity_comparison": equity_comparison,
        "sl_streak_comparison": streak_cmp,
        "drawdown_comparison": dd_cmp,
        "trade_comparison": trade_cmp,
        "reset_not_identifiable": False,
        "ref_be50_ge10": REF_BE50_GE10_DD,
    }
    payload["decision"] = decide(payload)
    return payload
