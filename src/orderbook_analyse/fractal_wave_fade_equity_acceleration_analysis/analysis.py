"""Orchestrate equity-acceleration decomposition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis import (
    AUDIT_VERSION,
    OUT_DIR_DEFAULT,
    REF_TRADES,
    SYMBOLS,
)
from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.metrics import (
    block_trade_stats,
    symbol_side_rows,
    tf_mix_rows,
    upgrade_rows,
)
from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.mfe_mae import (
    annotate_mfe_mae,
    load_1m_books,
    mfe_by_period,
)
from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.periods import assign_periods
from orderbook_analyse.fractal_wave_fade_equity_acceleration_analysis.volatility import (
    load_vol_frames,
    volatility_table,
)


def _year_group(period: str) -> str:
    return period.split("-")[0]


def _decide(
    half: pd.DataFrame,
    vol: pd.DataFrame,
    tf: pd.DataFrame,
    upg: pd.DataFrame,
    mfe: pd.DataFrame,
    sym_side: pd.DataFrame,
) -> dict[str, Any]:
    """Score candidate drivers comparing pre-2024 vs 2024+ half-years."""
    pre = half[~half["period"].str.startswith(("2024", "2025", "2026"))]
    post = half[half["period"].str.startswith(("2024", "2025", "2026"))]
    # focus acceleration onset: 2023 vs 2024
    y23 = half[half["period"].str.startswith("2023")]
    y24 = half[half["period"].str.startswith("2024")]
    y25 = half[half["period"].str.startswith("2025")]

    def wavg(df, col, wcol="trades"):
        if df.empty or df[col].isna().all():
            return None
        w = df[wcol].astype(float)
        x = df[col].astype(float)
        m = x.notna() & w.notna() & (w > 0)
        if not m.any():
            return None
        return float(np.average(x[m], weights=w[m]))

    def mean_col(df, col):
        if df.empty or df[col].isna().all():
            return None
        return float(df[col].astype(float).mean())

    drivers: dict[str, Any] = {}

    # A MORE_TRADES
    tpm_23 = mean_col(y23, "trades_per_month")
    tpm_24 = mean_col(y24, "trades_per_month")
    tpm_25 = mean_col(y25, "trades_per_month")
    drivers["A_MORE_TRADES"] = {
        "trades_per_month_2023": tpm_23,
        "trades_per_month_2024": tpm_24,
        "trades_per_month_2025": tpm_25,
        "delta_2024_vs_2023_pct": (
            (tpm_24 / tpm_23 - 1.0) * 100.0 if tpm_23 and tpm_24 else None
        ),
        "supports": bool(tpm_24 and tpm_23 and tpm_24 > tpm_23 * 1.15),
    }

    # B HIGHER_WIN_RATE
    wr23, wr24, wr25 = mean_col(y23, "win_rate"), mean_col(y24, "win_rate"), mean_col(y25, "win_rate")
    tp23, tp24 = mean_col(y23, "tp_rate"), mean_col(y24, "tp_rate")
    drivers["B_HIGHER_WIN_RATE"] = {
        "win_rate_2023": wr23,
        "win_rate_2024": wr24,
        "win_rate_2025": wr25,
        "tp_rate_2023": tp23,
        "tp_rate_2024": tp24,
        "delta_pp_2024_vs_2023": (wr24 - wr23) * 100.0 if wr23 is not None and wr24 is not None else None,
        "supports": bool(wr24 is not None and wr23 is not None and (wr24 - wr23) >= 0.02),
    }

    # C LARGER_WINNERS / higher edge per trade
    mw23, mw24 = mean_col(y23, "mean_winning_trade"), mean_col(y24, "mean_winning_trade")
    medw23, medw24 = mean_col(y23, "median_winning_trade"), mean_col(y24, "median_winning_trade")
    ml23, ml24 = mean_col(y23, "mean_losing_trade"), mean_col(y24, "mean_losing_trade")
    exp23, exp24, exp25 = mean_col(y23, "expectancy"), mean_col(y24, "expectancy"), mean_col(y25, "expectancy")
    larger_wins = bool(mw24 and mw23 and mw24 > mw23 * 1.08)
    higher_exp = bool(exp24 and exp23 and exp24 > exp23 * 1.1)
    drivers["C_LARGER_WINNERS"] = {
        "mean_win_2023": mw23,
        "mean_win_2024": mw24,
        "median_win_2023": medw23,
        "median_win_2024": medw24,
        "mean_loss_2023": ml23,
        "mean_loss_2024": ml24,
        "expectancy_2023": exp23,
        "expectancy_2024": exp24,
        "expectancy_2025": exp25,
        "winners_actually_larger": larger_wins,
        "note": (
            "Winner size flat/down; expectancy rise comes mainly from higher win/TP rate "
            "and mix — not bigger TP hits."
            if (not larger_wins and higher_exp)
            else "Check mean/median winning trade vs expectancy."
        ),
        "supports": larger_wins,  # only true larger winners; expectancy counted under B
    }

    # D TF MIX / UPGRADES
    share23 = mean_col(y23, "share_1h_4h")
    share24 = mean_col(y24, "share_1h_4h")
    up23 = mean_col(y23, "upgrade_rate")
    up24 = mean_col(y24, "upgrade_rate")
    # upgrade expectancy gap
    upg23 = upg[upg["period"].str.startswith("2023")]
    upg24 = upg[upg["period"].str.startswith("2024")]
    drivers["D_BETTER_TF_MIX_MORE_UPGRADES"] = {
        "share_1h_4h_2023": share23,
        "share_1h_4h_2024": share24,
        "upgrade_rate_2023": up23,
        "upgrade_rate_2024": up24,
        "expectancy_upgraded_2024": mean_col(upg24, "expectancy_upgraded"),
        "expectancy_not_upgraded_2024": mean_col(upg24, "expectancy_not_upgraded"),
        "supports": bool(
            (share24 and share23 and share24 > share23 + 0.05)
            or (up24 and up23 and up24 > up23 + 0.03)
        ),
    }

    # E VOLATILITY
    vol_both = vol[vol["symbol"] == "BOTH"]
    v23 = vol_both[vol_both["period"].str.startswith("2023")]
    v24 = vol_both[vol_both["period"].str.startswith("2024")]
    v25 = vol_both[vol_both["period"].str.startswith("2025")]
    atr23, atr24, atr25 = mean_col(v23, "median_atr14_pct"), mean_col(v24, "median_atr14_pct"), mean_col(v25, "median_atr14_pct")
    mfe23 = mean_col(mfe[mfe["period"].str.startswith("2023")], "median_mfe_pct")
    mfe24 = mean_col(mfe[mfe["period"].str.startswith("2024")], "median_mfe_pct")
    ratio23 = mean_col(mfe[mfe["period"].str.startswith("2023")], "median_mfe_over_tp")
    ratio24 = mean_col(mfe[mfe["period"].str.startswith("2024")], "median_mfe_over_tp")
    drivers["E_HIGHER_MARKET_VOLATILITY"] = {
        "median_atr14_pct_2023": atr23,
        "median_atr14_pct_2024": atr24,
        "median_atr14_pct_2025": atr25,
        "median_mfe_2023": mfe23,
        "median_mfe_2024": mfe24,
        "median_mfe_over_tp_2023": ratio23,
        "median_mfe_over_tp_2024": ratio24,
        "atr_delta_pct_2024_vs_2023": (
            (atr24 / atr23 - 1.0) * 100.0 if atr23 and atr24 else None
        ),
        "supports": bool(
            (atr24 and atr23 and atr24 > atr23 * 1.1)
            or (mfe24 and mfe23 and mfe24 > mfe23 * 1.1)
        ),
    }

    # Symbol split: both coins?
    ss24 = sym_side[(sym_side["period"].str.startswith("2024")) & (sym_side["side"] == "ALL")]
    ss23 = sym_side[(sym_side["period"].str.startswith("2023")) & (sym_side["side"] == "ALL")]
    apt_up = doge_up = False
    for sym in ("APTUSDT", "DOGEUSDT"):
        a23 = ss23[ss23["symbol"] == sym]["cumulative_additive"].sum()
        a24 = ss24[ss24["symbol"] == sym]["cumulative_additive"].sum()
        e23 = ss23[ss23["symbol"] == sym]["expectancy"].mean()
        e24 = ss24[ss24["symbol"] == sym]["expectancy"].mean()
        if (a24 > a23 * 1.2) or (e24 > e23 * 1.05 if pd.notna(e23) and pd.notna(e24) else False):
            if sym == "APTUSDT":
                apt_up = True
            else:
                doge_up = True

    supported = [k for k, v in drivers.items() if v.get("supports")]
    # Rank by magnitude of related metric shifts
    scores = {
        "A_MORE_TRADES": abs(drivers["A_MORE_TRADES"].get("delta_2024_vs_2023_pct") or 0),
        "B_HIGHER_WIN_RATE": abs(drivers["B_HIGHER_WIN_RATE"].get("delta_pp_2024_vs_2023") or 0) * 5
        + (
            ((exp24 / exp23 - 1) * 100) if exp23 and exp24 else 0
        ),
        "C_LARGER_WINNERS": (
            ((mw24 / mw23 - 1) * 100) if mw23 and mw24 and drivers["C_LARGER_WINNERS"]["supports"] else 0
        ),
        "D_BETTER_TF_MIX_MORE_UPGRADES": (
            abs((share24 or 0) - (share23 or 0)) * 100
            + abs((up24 or 0) - (up23 or 0)) * 100
        ),
        "E_HIGHER_MARKET_VOLATILITY": abs(
            drivers["E_HIGHER_MARKET_VOLATILITY"].get("atr_delta_pct_2024_vs_2023") or 0
        ),
    }
    # also weight cumulative additive jump
    cum23 = float(y23["cumulative_additive_return"].sum()) if len(y23) else 0
    cum24 = float(y24["cumulative_additive_return"].sum()) if len(y24) else 0

    if len(supported) >= 3:
        decision = "EQUITY_ACCELERATION_MULTI_FACTOR"
    elif len(supported) == 0:
        decision = "EQUITY_ACCELERATION_MULTI_FACTOR"
    else:
        top = max(supported, key=lambda k: scores.get(k, 0))
        decision = {
            "A_MORE_TRADES": "EQUITY_ACCELERATION_MAINLY_MORE_OPPORTUNITIES",
            "B_HIGHER_WIN_RATE": "EQUITY_ACCELERATION_MAINLY_HIGHER_SIGNAL_EDGE",
            "C_LARGER_WINNERS": "EQUITY_ACCELERATION_MAINLY_HIGHER_SIGNAL_EDGE",
            "D_BETTER_TF_MIX_MORE_UPGRADES": "EQUITY_ACCELERATION_MAINLY_TF_MIX_AND_UPGRADES",
            "E_HIGHER_MARKET_VOLATILITY": "EQUITY_ACCELERATION_MAINLY_HIGHER_VOLATILITY",
        }[top]
        # if top two close → multi
        ranked = sorted(supported, key=lambda k: scores.get(k, 0), reverse=True)
        if len(ranked) >= 2 and scores[ranked[1]] >= 0.7 * max(scores[ranked[0]], 1e-9):
            decision = "EQUITY_ACCELERATION_MULTI_FACTOR"

    return {
        "decision": decision,
        "supported_drivers": supported,
        "driver_scores": scores,
        "cumulative_additive_2023": cum23,
        "cumulative_additive_2024": cum24,
        "cumulative_additive_2025": float(y25["cumulative_additive_return"].sum()) if len(y25) else 0,
        "both_symbols_accelerate": bool(apt_up and doge_up),
        "apt_accelerates": apt_up,
        "doge_accelerates": doge_up,
        "drivers": drivers,
        "pre_2024_expectancy": wavg(pre, "expectancy"),
        "post_2024_expectancy": wavg(post, "expectancy"),
    }


def run_analysis(
    *,
    trades_path: Path = REF_TRADES,
    out_dir: Path = OUT_DIR_DEFAULT,
) -> dict[str, Any]:
    trades = pd.read_csv(trades_path)
    trades = assign_periods(trades)
    data_start = trades["exit_time"].min()
    data_end = trades["exit_time"].max()
    periods = sorted(trades["period"].unique().tolist())

    half_rows = []
    tf_rows = []
    up_rows = []
    ss_rows = []
    for period in periods:
        g = trades[trades["period"] == period]
        half_rows.append(
            block_trade_stats(g, period=period, data_start=data_start, data_end=data_end)
        )
        tf_rows.extend(tf_mix_rows(g, period))
        up_rows.append(upgrade_rows(g, period))
        ss_rows.extend(symbol_side_rows(g, period))

    half = pd.DataFrame(half_rows)
    tf = pd.DataFrame(tf_rows)
    # flatten highest_tf for csv
    upg = pd.DataFrame(up_rows)
    upg["highest_tf_counts_json"] = upg["highest_tf_counts"].map(
        lambda d: json.dumps(d) if isinstance(d, dict) else "{}"
    )
    upg_csv = upg.drop(columns=["highest_tf_counts"])
    sym_side = pd.DataFrame(ss_rows)

    frames = load_vol_frames(SYMBOLS)
    vol = volatility_table(periods, SYMBOLS, frames)

    books = load_1m_books(SYMBOLS)
    trades_mfe = annotate_mfe_mae(trades, books)
    mfe = mfe_by_period(trades_mfe)

    # merge key vol/mfe into halfyear for summary table
    vol_both = vol[vol["symbol"] == "BOTH"][["period", "median_atr14_pct"]].rename(
        columns={"median_atr14_pct": "median_atr14_pct"}
    )
    half2 = half.merge(vol_both, on="period", how="left")
    half2 = half2.merge(
        mfe[["period", "median_mfe_pct", "median_mfe_over_tp"]], on="period", how="left"
    )

    decision = _decide(half, vol, tf, upg, mfe, sym_side)

    payload = {
        "audit_version": AUDIT_VERSION,
        "trades_path": str(trades_path),
        "data_start": data_start,
        "data_end": data_end,
        "periods": periods,
        "halfyear": half2,
        "tf_mix": tf,
        "upgrade": upg_csv,
        "symbol_side": sym_side,
        "volatility": vol,
        "mfe": mfe,
        "decision": decision,
        "out_dir": out_dir,
    }
    return payload
