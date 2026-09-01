"""Economic comparison helpers. OPEN excluded from winrate and PnL sums."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from .config import FEE_PP


def closed(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.loc[frame["outcome"].isin(["WIN", "LOSS"])].copy()


def longest_loss_streak(frame: pd.DataFrame) -> int:
    part = closed(frame).sort_values(["entry_time", "signal_id"])
    best = 0
    cur = 0
    for outcome in part["outcome"].to_list():
        if outcome == "LOSS":
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def pnl_metrics(frame: pd.DataFrame, *, variant: str) -> dict[str, Any]:
    all_n = int(len(frame))
    wins = int((frame["outcome"] == "WIN").sum())
    losses = int((frame["outcome"] == "LOSS").sum())
    opens = int((frame["outcome"] == "OPEN").sum()) if "outcome" in frame else int(frame.get("is_open", pd.Series(dtype=bool)).sum())
    cl = closed(frame)
    gross = pd.to_numeric(cl["pnl_pct_gross"], errors="coerce")
    net = pd.to_numeric(cl["pnl_pct_net"], errors="coerce")
    if net.isna().all() and "pnl_pct_gross" in cl:
        net = gross - FEE_PP
    gp = float(gross[gross > 0].sum()) if len(gross) else 0.0
    gl = float(gross[gross < 0].sum()) if len(gross) else 0.0
    np_ = float(net[net > 0].sum()) if len(net) else 0.0
    nl = float(net[net < 0].sum()) if len(net) else 0.0
    gsum = float(gross.sum()) if len(gross) else 0.0
    nsum = float(net.sum()) if len(net) else 0.0
    gpf = (gp / abs(gl)) if gl != 0 else None
    npf = (np_ / abs(nl)) if nl != 0 else None
    fee_total = float(len(cl) * FEE_PP)
    return {
        "variant": variant,
        "trades": all_n,
        "wins": wins,
        "losses": losses,
        "open": opens,
        "closed": int(len(cl)),
        "winrate": (wins / (wins + losses)) if (wins + losses) else None,
        "gross_profit": gp,
        "gross_loss": gl,
        "gross_sum": gsum,
        "gross_pf": gpf,
        "fee_pp": FEE_PP,
        "fees_total_pp": fee_total,
        "net_profit": np_,
        "net_loss": nl,
        "net_sum": nsum,
        "net_pf": npf,
        "net_mean": float(net.mean()) if len(net) else None,
        "net_median": float(net.median()) if len(net) else None,
        "longest_loss_streak": longest_loss_streak(frame),
    }


def group_net(frame: pd.DataFrame, key: str) -> pd.DataFrame:
    rows = []
    cl = closed(frame)
    if cl.empty:
        return pd.DataFrame()
    cl = cl.copy()
    cl["_net"] = pd.to_numeric(cl["pnl_pct_net"], errors="coerce")
    for name, part in cl.groupby(key, dropna=False):
        m = pnl_metrics(part, variant=str(name))
        m["group"] = key
        m["bucket"] = str(name)
        rows.append(m)
    return pd.DataFrame(rows)


def blocked_summary(blocked: pd.DataFrame) -> dict[str, Any]:
    wins = blocked.loc[blocked["outcome"] == "WIN"]
    losses = blocked.loc[blocked["outcome"] == "LOSS"]
    opens = blocked.loc[blocked["outcome"] == "OPEN"]
    g_win = float(pd.to_numeric(wins["pnl_pct_gross"], errors="coerce").sum()) if len(wins) else 0.0
    g_loss = float(pd.to_numeric(losses["pnl_pct_gross"], errors="coerce").sum()) if len(losses) else 0.0
    n_win = float(pd.to_numeric(wins["pnl_pct_net"], errors="coerce").sum()) if len(wins) else 0.0
    n_loss = float(pd.to_numeric(losses["pnl_pct_net"], errors="coerce").sum()) if len(losses) else 0.0
    return {
        "blocked_trades": int(len(blocked)),
        "blocked_wins": int(len(wins)),
        "blocked_losses": int(len(losses)),
        "blocked_open": int(len(opens)),
        "missed_gross_profit": g_win,
        "avoided_gross_loss": -g_loss,
        "missed_net_profit": n_win,
        "avoided_net_loss": -n_loss,
        "blocked_loss_to_win_ratio": (len(losses) / len(wins)) if len(wins) else None,
        "net_effect_if_block": (-n_win) + (-n_loss) * 0 + (-n_win - n_loss),
        "net_sum_removed": n_win + n_loss,
    }


def horizon_summary(paths: pd.DataFrame, *, cohort: str, horizon: str) -> dict[str, Any]:
    status_col = f"{horizon}_status"
    ok = paths.loc[paths.get(status_col, pd.Series(dtype=object)) == "OK"].copy() if status_col in paths.columns else pd.DataFrame()
    n_all = int(len(paths))
    n_ok = int(len(ok))
    n_unavail = int((paths[status_col] == "HORIZON_UNAVAILABLE").sum()) if status_col in paths.columns else n_all
    if ok.empty:
        return {
            "cohort": cohort,
            "horizon": horizon,
            "n": n_all,
            "n_ok": 0,
            "n_unavailable": n_unavail,
            "share_in_direction": None,
            "median_aligned_return": None,
            "median_market_mfe": None,
            "median_market_mae": None,
            "share_tp_touched_in_trade": None,
            "share_sl_touched_in_trade": None,
            "share_still_open": None,
        }
    aligned = pd.to_numeric(ok[f"{horizon}_aligned_return_pct"], errors="coerce")
    return {
        "cohort": cohort,
        "horizon": horizon,
        "n": n_all,
        "n_ok": n_ok,
        "n_unavailable": n_unavail,
        "share_in_direction": float((ok[f"{horizon}_in_direction"] == True).mean()),
        "median_aligned_return": float(aligned.median()) if aligned.notna().any() else None,
        "mean_aligned_return": float(aligned.mean()) if aligned.notna().any() else None,
        "median_market_mfe": float(pd.to_numeric(ok[f"{horizon}_market_mfe_pct"], errors="coerce").median()),
        "median_market_mae": float(pd.to_numeric(ok[f"{horizon}_market_mae_pct"], errors="coerce").median()),
        "share_tp_touched_in_trade": float(ok[f"{horizon}_in_trade_tp_touched"].astype(bool).mean()),
        "share_sl_touched_in_trade": float(ok[f"{horizon}_in_trade_sl_touched"].astype(bool).mean()),
        "share_still_open": float(ok[f"{horizon}_still_open"].astype(bool).mean()),
    }


def recovery_stats(decisions: pd.DataFrame, paths: pd.DataFrame, horizon: str) -> dict[str, Any]:
    merged = decisions.merge(paths, on="signal_id", how="left", suffixes=("", "_path"))
    entry = pd.to_datetime(merged["entry_time"], utc=True)
    exit_t = pd.to_datetime(merged["exit_time"], utc=True)
    horizon_td = pd.to_timedelta({"4h": "4h", "6h": "6h"}[horizon])
    sl_early = (
        (merged["exit_reason"] == "SL")
        & exit_t.notna()
        & (exit_t <= entry + horizon_td)
    )
    ok = merged[f"{horizon}_status"] == "OK"
    recov = sl_early & ok & (pd.to_numeric(merged[f"{horizon}_aligned_return_pct"], errors="coerce") > 0)
    first_adv_then_tp = (
        ok
        & (merged[f"{horizon}_market_first_touch"] == "TP")
        & (pd.to_numeric(merged[f"{horizon}_market_mae_pct"], errors="coerce") > 0)
        & (merged["outcome"] == "WIN")
    )
    # first against then TP: MAE time before TP — approximate with first_touch TP and mae>0 on market path
    first_fav_then_sl = (
        (merged["outcome"] == "LOSS")
        & ok
        & (pd.to_numeric(merged[f"{horizon}_market_mfe_pct"], errors="coerce") > 0)
    )
    return {
        "horizon": horizon,
        "n_sl_before_horizon": int(sl_early.sum()),
        "n_sl_before_horizon_then_aligned": int(recov.sum()),
        "share_sl_early_that_recover": float(recov.sum() / sl_early.sum()) if int(sl_early.sum()) else None,
        "n_win_with_mae_then_tp": int(first_adv_then_tp.sum()),
        "n_loss_with_mfe_then_sl": int(first_fav_then_sl.sum()),
    }
