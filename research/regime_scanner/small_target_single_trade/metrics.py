"""Metrics and candidate gates for small-target audit."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

TOP3 = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
MAJORS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}


def metrics_block(df: pd.DataFrame, *, pnl_col: str = "net_pnl_pct") -> dict[str, Any]:
    if df is None or len(df) == 0:
        return {
            "n": 0,
            "expectation": None,
            "gross_expectation": None,
            "pf": None,
            "sum_pnl": 0.0,
            "winrate": None,
            "max_dd": 0.0,
            "max_losing_streak": 0,
            "median_hold": None,
            "mean_hold": None,
            "median_winner": None,
            "median_loser": None,
            "avg_winner": None,
            "avg_loser": None,
            "payoff_ratio": None,
            "be_winrate": None,
            "mean_mfe": None,
            "mean_mae": None,
            "mfe_mae_ratio": None,
            "tp_share": None,
            "sl_share": None,
            "time_exit_share": None,
            "data_end_share": None,
            "same_bar_share": None,
        }
    pnls = pd.to_numeric(df[pnl_col], errors="coerce").to_numpy(dtype=float)
    pnls = pnls[np.isfinite(pnls)]
    gross = pd.to_numeric(df.get("gross_pnl_pct"), errors="coerce").to_numpy(dtype=float)
    gross = gross[np.isfinite(gross)] if gross is not None and len(gross) else pnls
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    win_sum = float(wins.sum()) if len(wins) else 0.0
    loss_sum = float(-losses.sum()) if len(losses) else 0.0
    pf = None if loss_sum < 1e-15 else float(win_sum / loss_sum)
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq) if len(eq) else np.array([0.0])
    dd = float((eq - peak).min()) if len(eq) else 0.0
    streak = best = 0
    for x in pnls:
        if x < 0:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0
    held = pd.to_numeric(df.get("bars_held"), errors="coerce")
    avg_w = float(wins.mean()) if len(wins) else None
    avg_l = float(losses.mean()) if len(losses) else None
    payoff = None if avg_l is None or abs(avg_l) < 1e-15 or avg_w is None else float(avg_w / abs(avg_l))
    # break-even winrate for average win/loss
    be = None
    if avg_w is not None and avg_l is not None and (avg_w - avg_l) != 0:
        be = float(abs(avg_l) / (avg_w + abs(avg_l)))
    reasons = df["exit_reason"].value_counts(normalize=True) if "exit_reason" in df.columns else pd.Series(dtype=float)
    mfe = pd.to_numeric(df.get("mfe_pct"), errors="coerce")
    mae = pd.to_numeric(df.get("mae_pct"), errors="coerce")
    mae_abs = mae.abs()
    ratio = None
    if mfe.notna().any() and mae_abs.notna().any() and float(mae_abs.mean() or 0) > 1e-15:
        ratio = float(mfe.mean() / mae_abs.mean())
    cost = float(pd.to_numeric(df.get("effective_cost_pct"), errors="coerce").iloc[0]) if "effective_cost_pct" in df.columns and len(df) else 0.2
    gross_pos = float(gross[gross > 0].sum()) if len(gross) else 0.0
    cost_drag = float(np.sum(np.full(len(pnls), cost))) if len(pnls) else 0.0
    cost_share = None if gross_pos < 1e-15 else float(cost_drag / gross_pos)
    return {
        "n": int(len(pnls)),
        "expectation": float(np.mean(pnls)) if len(pnls) else None,
        "gross_expectation": float(np.mean(gross)) if len(gross) else None,
        "pf": pf,
        "sum_pnl": float(np.sum(pnls)) if len(pnls) else 0.0,
        "winrate": float(np.mean(pnls > 0)) if len(pnls) else None,
        "max_dd": dd,
        "max_losing_streak": int(best),
        "median_hold": float(held.median()) if held.notna().any() else None,
        "mean_hold": float(held.mean()) if held.notna().any() else None,
        "median_winner": float(np.median(wins)) if len(wins) else None,
        "median_loser": float(np.median(losses)) if len(losses) else None,
        "avg_winner": avg_w,
        "avg_loser": avg_l,
        "payoff_ratio": payoff,
        "be_winrate": be,
        "mean_mfe": float(mfe.mean()) if mfe.notna().any() else None,
        "mean_mae": float(mae.mean()) if mae.notna().any() else None,
        "mfe_mae_ratio": ratio,
        "tp_share": float(reasons.get("TP", 0.0)),
        "sl_share": float(reasons.get("SL", 0.0) + reasons.get("same_bar_conservative_sl", 0.0)),
        "time_exit_share": float(reasons.get("time_exit", 0.0)),
        "data_end_share": float(reasons.get("data_end", 0.0)),
        "same_bar_share": float(pd.to_numeric(df.get("same_bar_ambiguous"), errors="coerce").mean())
        if "same_bar_ambiguous" in df.columns
        else None,
        "cost_share_of_gross_wins": cost_share,
    }


def coin_expectations(df: pd.DataFrame) -> dict[str, float | None]:
    out = {}
    for sym, g in df.groupby(df.symbol.astype(str)):
        out[sym] = metrics_block(g)["expectation"]
    return out


def slice_pack(df: pd.DataFrame) -> dict[str, Any]:
    m = metrics_block(df)
    cex = coin_expectations(df)
    vals = [v for v in cex.values() if v is not None]
    m["equal_coin_expectation"] = float(np.mean(vals)) if vals else None
    m["median_coin_expectation"] = float(np.median(vals)) if vals else None
    m["pct_coins_positive"] = float(np.mean([v > 0 for v in vals])) if vals else None
    m["n_coins"] = len(cex)
    pos_sums = df.groupby(df.symbol.astype(str))["net_pnl_pct"].sum()
    pos = pos_sums[pos_sums > 0]
    m["max_coin_pnl_share"] = float(pos.max() / pos.sum()) if len(pos) and pos.sum() > 0 else 0.0
    wins = df[pd.to_numeric(df.net_pnl_pct, errors="coerce") > 0]["net_pnl_pct"]
    m["top_winner_share"] = (
        float(wins.nlargest(max(1, len(wins) // 10)).sum() / wins.sum()) if len(wins) and wins.sum() > 0 else 0.0
    )
    return m


def evaluate_gates(
    indep: dict[str, Any],
    sequential: dict[str, Any],
    *,
    cost025: dict[str, Any] | None,
    cost030: dict[str, Any] | None,
    tp: float,
) -> dict[str, Any]:
    def pos(x):
        return x is not None and float(x) > 0

    def nonneg(x):
        return x is not None and float(x) >= 0

    checks = {
        "net_e_pos_020": pos(indep.get("expectation")),
        "pf_gt_1": indep.get("pf") is not None and float(indep["pf"]) > 1.0,
        "oos_not_neg": nonneg(indep.get("oos_expectation")),
        "val_oos_not_opposed": True,
        "equal_coin_pos": pos(indep.get("equal_coin_expectation")),
        "median_coin_nonneg": nonneg(indep.get("median_coin_expectation")),
        "common_window_pos": pos(indep.get("common_window_expectation")),
        "without_apt_pos": pos(indep.get("without_apt_expectation")),
        "without_top3_nonneg": nonneg(indep.get("without_top3_expectation")),
        "pct_coins_ge_60": (indep.get("pct_coins_positive") or 0) >= 0.60,
        "no_coin_dominance": (indep.get("max_coin_pnl_share") or 1) <= 0.60,
        "enough_n": int(indep.get("n") or 0) >= 40,
        "sequential_pos": pos(sequential.get("expectation")),
        "dd_ok": indep.get("max_dd") is not None and float(indep["max_dd"]) > -80.0,
        "streak_ok": int(indep.get("max_losing_streak") or 999) <= 25,
        "not_only_time_exit": (indep.get("time_exit_share") or 0) <= 0.50,
        "not_extreme_winners": (indep.get("top_winner_share") or 1) <= 0.45,
        "cost_share_ok": indep.get("cost_share_of_gross_wins") is None
        or float(indep["cost_share_of_gross_wins"]) <= 2.0,
        "slip_025_nonneg": True if cost025 is None else nonneg(cost025.get("expectation")),
        "slip_030_not_collapse": True
        if cost030 is None or indep.get("expectation") is None
        else (
            cost030.get("expectation") is not None
            and float(cost030["expectation"]) > float(indep["expectation"]) - 0.15
        ),
    }
    vo, oo = indep.get("validation_expectation"), indep.get("oos_expectation")
    if vo is not None and oo is not None:
        checks["val_oos_not_opposed"] = not (
            (float(vo) > 0.05 and float(oo) < -0.05) or (float(oo) > 0.05 and float(vo) < -0.05)
        )
    # micro TP special
    micro_ok = True
    if abs(float(tp) - 0.25) < 1e-12:
        micro_ok = all(
            [
                checks["net_e_pos_020"],
                checks["slip_025_nonneg"],
                checks["sequential_pos"],
                checks["pf_gt_1"],
                checks["pct_coins_ge_60"],
                checks["oos_not_neg"],
                (indep.get("same_bar_share") or 0) <= 0.35,
            ]
        )
    checks["micro_target_ok"] = micro_ok if abs(float(tp) - 0.25) < 1e-12 else True
    core = [
        "net_e_pos_020",
        "pf_gt_1",
        "oos_not_neg",
        "val_oos_not_opposed",
        "equal_coin_pos",
        "median_coin_nonneg",
        "common_window_pos",
        "without_apt_pos",
        "without_top3_nonneg",
        "pct_coins_ge_60",
        "no_coin_dominance",
        "enough_n",
        "sequential_pos",
        "dd_ok",
        "streak_ok",
        "not_only_time_exit",
        "not_extreme_winners",
        "cost_share_ok",
        "micro_target_ok",
    ]
    checks["pass"] = all(checks[k] for k in core)
    return checks
