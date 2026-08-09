"""Orchestrate cashout/reserve analysis on validated trades."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis import (
    AUDIT_VERSION,
    CASHOUT_RATES,
    DEFINITIONS_DOC,
    EXPECTED_N_TRADES,
    REF_TRADES,
    START_ACTIVE,
)
from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis.simulate import simulate_cashout
from orderbook_analyse.fractal_wave_fade_cashout_reserve_analysis.streaks import (
    find_streaks,
    losing_predicate,
    sl_predicate,
    streak_impact_on_paths,
    subset_max_sl,
    worst_blocks,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def load_trades() -> pd.DataFrame:
    path = _repo_root() / REF_TRADES
    df = pd.read_csv(path)
    for c in ("entry_time", "exit_time", "signal_time"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c], utc=True)
    df = df.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    return df


def _trade_controls(df: pd.DataFrame) -> dict[str, Any]:
    nets = df["net_return_pct"].astype(float).to_numpy()
    wins = nets[nets > 1e-12]
    losses = nets[nets < -1e-12]
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    pf = (
        float(np.sum(wins) / abs(np.sum(losses)))
        if len(wins) and len(losses) and np.sum(losses) != 0
        else None
    )
    return {
        "n_trades": int(len(df)),
        "tp": int((df["exit_reason"] == "TP").sum()),
        "sl": int((df["exit_reason"] == "SL").sum()),
        "timeout": int((df["exit_reason"] == "TIMEOUT").sum()),
        "conflict": int((df["exit_reason"] == "HIGHER_TF_CONFLICT").sum()),
        "expectancy": float(np.mean(nets)),
        "profit_factor": pf,
        "max_drawdown_additive": float(dd.min()) if len(dd) else 0.0,
        "cumulative_additive_net": float(np.sum(nets)),
        "order_ok": bool(
            (
                df["exit_time"].astype("datetime64[ns, UTC]")
                .diff()
                .dropna()
                >= pd.Timedelta(0)
            ).all()
        ),
    }


def run_analysis() -> dict[str, Any]:
    print(DEFINITIONS_DOC, flush=True)
    trades = load_trades()
    assert len(trades) == EXPECTED_N_TRADES, f"expected {EXPECTED_N_TRADES}, got {len(trades)}"
    controls = _trade_controls(trades)
    print(
        f"[controls] n={controls['n_trades']} TP={controls['tp']} SL={controls['sl']} "
        f"exp={controls['expectancy']:.6f} PF={controls['profit_factor']:.6f} "
        f"addDD={controls['max_drawdown_additive']:.4f}",
        flush=True,
    )

    # streaks on raw trade sequence
    sl_info = find_streaks(trades, predicate=sl_predicate, label="SL")
    lose_info = find_streaks(trades, predicate=losing_predicate, label="LOSING")
    print(
        f"[streaks] max_SL={sl_info['max_length']} max_losing={lose_info['max_length']}",
        flush=True,
    )

    sl_by_symbol = {
        "APTUSDT": subset_max_sl(trades, trades["symbol"] == "APTUSDT"),
        "DOGEUSDT": subset_max_sl(trades, trades["symbol"] == "DOGEUSDT"),
    }
    sl_by_side = {
        "LONG": subset_max_sl(trades, trades["side"] == "LONG"),
        "SHORT": subset_max_sl(trades, trades["side"] == "SHORT"),
    }
    sl_by_tf = {
        tf: subset_max_sl(trades, trades["first_signal_tf"] == tf)
        for tf in ("15m", "30m", "1h", "4h")
    }

    blocks = worst_blocks(trades)

    sims = {}
    paths = {}
    summaries = []
    for rate in CASHOUT_RATES:
        print(f"[sim] cashout {int(rate*100)}% …", flush=True)
        res = simulate_cashout(trades, rate)
        sims[rate] = res
        paths[rate] = res["path"]
        summaries.append(res["summary"])

    # verify 0% matches equity_after_100 if present
    parity = None
    if "equity_after_100" in trades.columns:
        p0 = paths[0.0]
        ref = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
        a = p0["active_after"].astype(float).to_numpy()
        b = ref["equity_after_100"].astype(float).to_numpy()
        # Large compounded magnitudes: allow tiny absolute drift (~cents on billions)
        end_ok = abs(float(a[-1]) - float(b[-1])) <= max(1.0, 1e-9 * abs(float(b[-1])))
        path_ok = bool(np.allclose(a, b, rtol=1e-9, atol=1.0))
        parity = bool(end_ok and path_ok)
        print(
            f"[parity] 0% vs equity_after_100: {parity} "
            f"(end_diff={float(a[-1]-b[-1]):.6f})",
            flush=True,
        )

    impact = streak_impact_on_paths(trades, sl_info["worst"], paths)

    comparison = pd.DataFrame(
        [
            {
                "cashout_rate": s["cashout_rate"],
                "cashout_rate_pct": s["cashout_rate_pct"],
                "end_active": s["end_active"],
                "end_reserve": s["end_reserve"],
                "end_total_wealth": s["end_total_wealth"],
                "total_wealth_return_pct": s["total_wealth_return_pct"],
                "active_equity_return_pct": s["active_equity_return_pct"],
                "total_cashed_out": s["total_cashed_out"],
                "n_cashouts": s["n_cashouts"],
                "avg_cashout_per_win": s["avg_cashout_per_win"],
                "active_max_dd_pct": s["active_max_dd_pct"],
                "active_max_dd_usdt": s["active_max_dd_usdt"],
                "total_max_dd_pct": s["total_max_dd_pct"],
                "total_max_dd_usdt": s["total_max_dd_usdt"],
                "reserve_at_worst_dd": s["reserve_at_worst_active_dd"],
                "reserve_before_worst_dd": s["reserve_before_worst_active_dd"],
                "active_peak_to_trough_loss": s["active_peak_to_trough_loss"],
                "coverage_ratio": s["coverage_ratio"],
                "RESERVE_COVERS_MAX_DD": s["RESERVE_COVERS_MAX_DD"],
                "max_sl_streak": sl_info["max_length"],
                "max_losing_streak": lose_info["max_length"],
            }
            for s in summaries
        ]
    )

    # long equity path table (all rates side by side would be huge) — store per-rate paths concatenated
    equity_paths = pd.concat(
        [paths[r].assign(cashout_rate_pct=int(round(r * 100))) for r in CASHOUT_RATES],
        ignore_index=True,
    )

    dd_rows = []
    for s in summaries:
        for kind, dd in (("active", s["dd_active"]), ("total", s["dd_total"])):
            dd_rows.append(
                {
                    "cashout_rate_pct": s["cashout_rate_pct"],
                    "equity_kind": kind,
                    **{k: v for k, v in dd.items() if k not in ("peak_i", "trough_i")},
                }
            )
    dd_df = pd.DataFrame(dd_rows)

    sl_streaks_df = pd.DataFrame(sl_info["all_streaks"])
    lose_streaks_df = pd.DataFrame(lose_info["all_streaks"])
    dist_df = pd.DataFrame(sl_info["distribution"] + lose_info["distribution"])

    interpretation = (
        "Cashout does not change the trading edge (same trades/returns). "
        "Higher cashout rates secure more capital in reserve and reduce total-wealth "
        "drawdown exposure, at the cost of slower active compounding. "
        "Reserve improves survivability of the capital path; it does not improve expectancy/PF."
    )

    return {
        "audit_version": AUDIT_VERSION,
        "definitions": DEFINITIONS_DOC,
        "controls": controls,
        "parity_0pct_vs_equity_after_100": parity,
        "sl_streak": sl_info,
        "losing_streak": lose_info,
        "sl_by_symbol": sl_by_symbol,
        "sl_by_side": sl_by_side,
        "sl_by_tf": sl_by_tf,
        "comparison": comparison,
        "equity_paths": equity_paths,
        "drawdown_comparison": dd_df,
        "sl_streaks": sl_streaks_df,
        "losing_streaks": lose_streaks_df,
        "streak_distribution": dist_df,
        "worst_trade_blocks": blocks,
        "worst_sl_streak_cashout_impact": impact,
        "summaries": summaries,
        "interpretation": interpretation,
        "start_active": START_ACTIVE,
    }
