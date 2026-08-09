"""Deterministic sample selection from final trades."""

from __future__ import annotations

import numpy as np
import pandas as pd


def _utc(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s, utc=True)


def load_trades(path) -> pd.DataFrame:
    df = pd.read_csv(path)
    for c in ("signal_time", "entry_time", "exit_time"):
        df[c] = _utc(df[c])
    return df


def window_trades(
    trades: pd.DataFrame,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> pd.DataFrame:
    m = (trades["entry_time"] >= start) & (trades["entry_time"] <= end)
    return trades.loc[m].sort_values(["entry_time", "trade_id"]).reset_index(drop=True)


def ensure_min_trades(
    trades: pd.DataFrame,
    *,
    primary_start: pd.Timestamp,
    primary_end: pd.Timestamp,
    min_n: int = 10,
) -> tuple[pd.DataFrame, pd.Timestamp, pd.Timestamp, str]:
    """Use July window; extend backward only if fewer than min_n trades."""
    pool = window_trades(trades, start=primary_start, end=primary_end)
    note = "PRIMARY_JULY_2026"
    start = primary_start
    if len(pool) >= min_n:
        return pool, start, primary_end, note

    # extend backward day by day until enough
    cur = primary_start
    while len(pool) < min_n and cur > trades["entry_time"].min():
        cur = cur - pd.Timedelta(days=1)
        pool = window_trades(trades, start=cur, end=primary_end)
    note = f"EXTENDED_BACKWARD_TO_{cur.strftime('%Y-%m-%d')}"
    return pool, cur, primary_end, note


def _even_indices(n: int, k: int) -> list[int]:
    if n <= 0 or k <= 0:
        return []
    if k >= n:
        return list(range(n))
    return sorted({int(round(x)) for x in np.linspace(0, n - 1, k)})


def select_manual_sample(pool: pd.DataFrame, *, target_n: int = 10) -> pd.DataFrame:
    """
    Deterministic: 5 winners + 5 losers when possible (evenly spaced in each),
    then merge chronologically. Prefer diversity swaps for LONG/SHORT/upgrade
    without ranking by PnL magnitude.
    """
    if pool.empty:
        return pool.copy()

    wins = pool[pool["net_return_pct"].astype(float) > 0].reset_index(drop=True)
    losses = pool[pool["net_return_pct"].astype(float) <= 0].reset_index(drop=True)

    n_win = min(5, len(wins), target_n)
    n_loss = min(target_n - n_win, len(losses))
    # if not enough losses, fill from wins (or vice versa)
    if n_win + n_loss < target_n:
        need = target_n - (n_win + n_loss)
        if len(wins) > n_win:
            n_win = min(len(wins), n_win + need)
        elif len(losses) > n_loss:
            n_loss = min(len(losses), n_loss + need)

    wi = _even_indices(len(wins), n_win)
    li = _even_indices(len(losses), n_loss)
    selected = pd.concat([wins.iloc[wi], losses.iloc[li]], ignore_index=True)
    selected = selected.drop_duplicates(subset=["trade_id"]).sort_values(
        ["entry_time", "trade_id"]
    )

    # Diversity: ensure at least one LONG, SHORT, TP, SL, upgrade (if available in pool)
    def _has(df, col, val=None, pred=None):
        if pred is not None:
            return bool(pred(df).any()) if len(df) else False
        return bool((df[col] == val).any()) if len(df) else False

    def _try_swap(sel: pd.DataFrame, *, want_mask, pool_mask, same_sign: bool) -> pd.DataFrame:
        if want_mask(sel).any():
            return sel
        candidates = pool.loc[pool_mask(pool)].copy()
        if candidates.empty:
            return sel
        # replace a same-sign trade that duplicates side/reason if possible
        for i, row in sel.iterrows():
            net = float(row["net_return_pct"])
            if same_sign and ((net > 0) != (float(candidates.iloc[0]["net_return_pct"]) > 0)):
                continue
            # avoid removing the only instance of another scarce attribute later; simple replace
            repl = candidates.iloc[0]
            if int(repl["trade_id"]) in set(sel["trade_id"].astype(int)):
                # find first unused
                used = set(sel["trade_id"].astype(int))
                unused = candidates[~candidates["trade_id"].isin(used)]
                if unused.empty:
                    return sel
                repl = unused.iloc[0]
            sel = sel.copy()
            sel.loc[i] = repl
            return sel.sort_values(["entry_time", "trade_id"])
        return sel

    selected = _try_swap(
        selected,
        want_mask=lambda d: d["side"] == "LONG",
        pool_mask=lambda d: d["side"] == "LONG",
        same_sign=True,
    )
    selected = _try_swap(
        selected,
        want_mask=lambda d: d["side"] == "SHORT",
        pool_mask=lambda d: d["side"] == "SHORT",
        same_sign=True,
    )
    selected = _try_swap(
        selected,
        want_mask=lambda d: d["exit_reason"] == "TP",
        pool_mask=lambda d: d["exit_reason"] == "TP",
        same_sign=True,
    )
    selected = _try_swap(
        selected,
        want_mask=lambda d: d["exit_reason"] == "SL",
        pool_mask=lambda d: d["exit_reason"] == "SL",
        same_sign=True,
    )
    if (pool["upgrade_count"].astype(int) > 0).any():
        selected = _try_swap(
            selected,
            want_mask=lambda d: d["upgrade_count"].astype(int) > 0,
            pool_mask=lambda d: d["upgrade_count"].astype(int) > 0,
            same_sign=True,
        )

    # trim / pad to target_n while keeping chronological order
    selected = selected.drop_duplicates(subset=["trade_id"]).sort_values(
        ["entry_time", "trade_id"]
    )
    if len(selected) > target_n:
        # keep evenly spaced among already selected
        keep = _even_indices(len(selected), target_n)
        selected = selected.iloc[keep]
    elif len(selected) < target_n:
        used = set(selected["trade_id"].astype(int))
        fill = pool[~pool["trade_id"].isin(used)]
        fi = _even_indices(len(fill), target_n - len(selected))
        selected = pd.concat([selected, fill.iloc[fi]], ignore_index=True)

    return (
        selected.drop_duplicates(subset=["trade_id"])
        .sort_values(["entry_time", "trade_id"])
        .reset_index(drop=True)
    )
