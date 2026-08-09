"""Parity check: paper simulator vs frozen strategy backtest engine."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_signal_confluence_db.signals import frozen_eff_edges_all_signal_tfs
from orderbook_analyse.fractal_wave_fade_forward_paper.data import load_books, load_signals
from orderbook_analyse.fractal_wave_fade_forward_paper.simulator import simulate_symbol_paper
from orderbook_analyse.fractal_wave_fade_strategy_backtest_db.engine import run_symbol_backtest


def _utc(ts) -> pd.Timestamp:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def run_parity(
    symbol: str = "DOGEUSDT",
    *,
    window_start: str,
    window_end: str,
    fee_pct: float = 0.11,
    conflict_exit: bool = True,
) -> dict[str, Any]:
    """
    Compare paper simulator to backtest on trades with:
      window_start <= entry_time < window_end
      and exit_time < window_end  (fully resolved inside window)

    Paper uses early paper_start (= first 1m) so pre-window path state matches backtest.
    """
    edges = frozen_eff_edges_all_signal_tfs()
    books = load_books(symbol)
    sig = load_signals(symbol, books, edges)
    w0, w1 = _utc(window_start), _utc(window_end)
    early = _utc(pd.Timestamp(books.open_times[0]))

    bt = run_symbol_backtest(
        symbol,
        sig,
        books,
        tier_a_only=True,
        upgrade_policy="P5A",
        conflict_exit=conflict_exit,
        fee_pct=fee_pct,
        extra_4h=False,
    )
    bt_df = pd.DataFrame(bt["trades"])
    if not bt_df.empty:
        bt_df["entry_time"] = pd.to_datetime(bt_df["entry_time"], utc=True)
        bt_df["exit_time"] = pd.to_datetime(bt_df["exit_time"], utc=True)
        bt_df = bt_df[
            (bt_df["entry_time"] >= w0) & (bt_df["entry_time"] < w1) & (bt_df["exit_time"] < w1)
        ].copy()
        bt_df = bt_df.sort_values(["entry_time", "exit_time"]).reset_index(drop=True)

    paper = simulate_symbol_paper(
        symbol,
        sig,
        books,
        paper_start=early,
        forward_capture_start=None,
        fee_pct=fee_pct,
        conflict_exit=conflict_exit,
        trade_id_start=1,
        until_1m=w1,
        force_close_end=False,
    )
    p_df = pd.DataFrame(paper["trades"])
    if not p_df.empty:
        p_df["entry_time"] = pd.to_datetime(p_df["entry_time"], utc=True)
        p_df["exit_time"] = pd.to_datetime(p_df["exit_time"], utc=True)
        p_df = p_df[
            (p_df["entry_time"] >= w0) & (p_df["entry_time"] < w1) & (p_df["exit_time"] < w1)
        ].copy()
        p_df = p_df.sort_values(["entry_time", "exit_time"]).reset_index(drop=True)

    diffs: list[str] = []
    n_bt, n_p = len(bt_df), len(p_df)
    if n_bt != n_p:
        diffs.append(f"trade_count backtest={n_bt} paper={n_p}")

    n = min(n_bt, n_p)
    mismatch_entries = mismatch_exits = mismatch_nets = mismatch_upgrades = 0
    for i in range(n):
        b, p = bt_df.iloc[i], p_df.iloc[i]
        if abs((b["entry_time"] - p["entry_time"]).total_seconds()) > 0:
            mismatch_entries += 1
        if abs(float(b["entry_price"]) - float(p["entry_price"])) > 1e-10:
            mismatch_entries += 1
        if str(b["exit_reason"]) != str(p["exit_reason"]):
            mismatch_exits += 1
        if abs(float(b["net_return"]) - float(p["net_return_pct"])) > 1e-6:
            mismatch_nets += 1
        if int(b["number_of_upgrades"]) != int(p["upgrade_count"]):
            mismatch_upgrades += 1
        if abs((b["exit_time"] - p["exit_time"]).total_seconds()) > 0:
            mismatch_exits += 1

    if mismatch_entries:
        diffs.append(f"entry_mismatches={mismatch_entries}")
    if mismatch_exits:
        diffs.append(f"exit_mismatches={mismatch_exits}")
    if mismatch_nets:
        diffs.append(f"net_mismatches={mismatch_nets}")
    if mismatch_upgrades:
        diffs.append(f"upgrade_mismatches={mismatch_upgrades}")

    bt_up = int(bt_df["number_of_upgrades"].sum()) if n_bt else 0
    p_up = int(p_df["upgrade_count"].sum()) if n_p else 0
    if bt_up != p_up:
        diffs.append(f"total_upgrades backtest={bt_up} paper={p_up}")

    status = "PAPER_RUNNER_MATCHES_BACKTEST" if not diffs and n_bt > 0 else (
        "PAPER_RUNNER_PARITY_FAIL" if diffs else "PAPER_RUNNER_PARITY_FAIL"
    )
    if n_bt == 0 and n_p == 0:
        status = "PAPER_RUNNER_PARITY_FAIL"
        diffs.append("no_trades_in_window")

    return {
        "status": status,
        "symbol": symbol,
        "window_start": w0.isoformat(),
        "window_end": w1.isoformat(),
        "paper_warmup_start": early.isoformat(),
        "n_backtest": n_bt,
        "n_paper": n_p,
        "diffs": diffs,
        "sample_backtest_head": bt_df.head(3).to_dict("records") if n_bt else [],
        "sample_paper_head": p_df.head(3).to_dict("records") if n_p else [],
    }
