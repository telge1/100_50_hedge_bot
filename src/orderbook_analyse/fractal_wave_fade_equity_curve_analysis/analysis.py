"""Orchestrate leveraged Active/Reserve equity curve analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_curve_analysis import (
    AUDIT_VERSION,
    CASHOUT_RATE,
    COVERAGE_RATE,
    EXPECTED_N_TRADES,
    LEVERAGES,
    OUT_DIR_DEFAULT,
    REF_TRADES,
    START_ACTIVE,
    START_RESERVE,
)
from orderbook_analyse.fractal_wave_fade_equity_curve_analysis.export import write_results
from orderbook_analyse.fractal_wave_fade_equity_curve_analysis.plots import write_all_plots
from orderbook_analyse.fractal_wave_fade_equity_curve_analysis.simulate import (
    monthly_snapshots,
    reserve_events,
    simulate_levered_path,
)


def run_analysis(
    *,
    trades_path: Path = REF_TRADES,
    out_dir: Path = OUT_DIR_DEFAULT,
) -> dict[str, Any]:
    trades = pd.read_csv(trades_path)
    trades["exit_time"] = pd.to_datetime(trades["exit_time"], utc=True)
    trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True)
    trades = trades.sort_values(["exit_time", "trade_id"]).reset_index(drop=True)
    assert len(trades) == EXPECTED_N_TRADES, f"expected {EXPECTED_N_TRADES}, got {len(trades)}"

    paths_by_lev: dict[float, pd.DataFrame] = {}
    summaries: list[dict[str, Any]] = []
    monthly_frames: list[pd.DataFrame] = []
    event_frames: list[pd.DataFrame] = []

    for lev in LEVERAGES:
        print(f"[sim] leverage={lev}x …", flush=True)
        res = simulate_levered_path(
            trades,
            leverage=float(lev),
            cashout_rate=CASHOUT_RATE,
            coverage_rate=COVERAGE_RATE,
            start_active=START_ACTIVE,
            start_reserve=START_RESERVE,
        )
        paths_by_lev[float(lev)] = res["path"]
        summaries.append(res["summary"])
        monthly_frames.append(monthly_snapshots(res["path"], float(lev)))
        ev = reserve_events(res["path"])
        event_frames.append(ev)

    all_df = pd.concat(list(paths_by_lev.values()), ignore_index=True)
    # column order per spec
    col_order = [
        "trade_number",
        "trade_id",
        "exit_time",
        "symbol",
        "side",
        "exit_reason",
        "first_signal_tf",
        "net_return_pct",
        "leverage",
        "leveraged_net_return_pct",
        "active_before",
        "raw_trade_pnl",
        "cashout_amount",
        "reimbursement_amount",
        "active_after",
        "reserve_after",
        "total_wealth_after",
        "active_drawdown_pct",
        "total_drawdown_pct",
        "capital_depleted",
        "skipped_after_depletion",
    ]
    all_df = all_df[[c for c in col_order if c in all_df.columns]]

    monthly = pd.concat(monthly_frames, ignore_index=True)
    events = pd.concat(event_frames, ignore_index=True)
    summaries_df = pd.DataFrame(summaries)

    print("[plot] writing charts …", flush=True)
    plot_paths = write_all_plots(paths_by_lev, out_dir)

    summary = {
        "audit_version": AUDIT_VERSION,
        "trades_path": str(trades_path),
        "n_trades": int(len(trades)),
        "cashout_rate": CASHOUT_RATE,
        "coverage_rate": COVERAGE_RATE,
        "start_active": START_ACTIVE,
        "start_reserve": START_RESERVE,
        "leverages": list(LEVERAGES),
        "by_leverage": {str(int(s["leverage"])): s for s in summaries},
    }

    payload = {
        "equity_curve_all": all_df,
        "monthly": monthly,
        "reserve_events": events,
        "summaries_df": summaries_df,
        "summaries": summaries,
        "paths_by_lev": paths_by_lev,
        "plot_paths": {k: str(v) for k, v in plot_paths.items()},
        "summary": summary,
        "out_dir": out_dir,
    }
    write_results(payload, out_dir)
    return payload
