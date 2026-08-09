"""Plot Active / Reserve / Drawdown curves."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import pandas as pd

from orderbook_analyse.fractal_wave_fade_equity_curve_analysis import (
    WORST_SL_STREAK_END,
    WORST_SL_STREAK_START,
)


def _shade_worst_sl(ax) -> None:
    a = pd.Timestamp(WORST_SL_STREAK_START, tz="UTC")
    b = pd.Timestamp(WORST_SL_STREAK_END, tz="UTC")
    ax.axvspan(a, b, color="#c0392b", alpha=0.18, label="Worst 10-SL streak")


def _style_time_axis(ax) -> None:
    ax.grid(True, alpha=0.3)
    ax.tick_params(axis="x", rotation=30)


def plot_multi_leverage(
    paths_by_lev: dict[float, pd.DataFrame],
    *,
    y_col: str,
    title: str,
    ylabel: str,
    out_path: Path,
    log_y: bool = False,
    shade_worst_sl: bool = False,
) -> Path:
    fig, ax = plt.subplots(figsize=(12, 6))
    for lev in sorted(paths_by_lev.keys()):
        p = paths_by_lev[lev]
        # stop plotting after depletion for clarity (keep last zero point)
        if "capital_depleted" in p.columns and p["capital_depleted"].any():
            first_dep = int(p.index[p["capital_depleted"]][0])
            p = p.loc[:first_dep]
        y = p[y_col].astype(float)
        if log_y:
            y = y.clip(lower=1e-6)
        ax.plot(
            pd.to_datetime(p["exit_time"], utc=True),
            y,
            label=f"{int(lev)}x",
            linewidth=1.4,
        )
    if shade_worst_sl:
        _shade_worst_sl(ax)
    if log_y:
        ax.set_yscale("log")
    ax.set_title(title)
    ax.set_xlabel("Exit time (UTC)")
    ax.set_ylabel(ylabel)
    ax.legend(loc="best")
    _style_time_axis(ax)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def plot_single_leverage(
    path: pd.DataFrame,
    *,
    y_col: str,
    title: str,
    ylabel: str,
    out_path: Path,
    shade_worst_sl: bool = False,
    log_y: bool = False,
) -> Path:
    fig, ax = plt.subplots(figsize=(12, 6))
    p = path
    if "capital_depleted" in p.columns and p["capital_depleted"].any():
        first_dep = int(p.index[p["capital_depleted"]][0])
        p = p.loc[:first_dep]
    ax.plot(
        pd.to_datetime(p["exit_time"], utc=True),
        p[y_col].astype(float),
        color="#1f77b4" if "active" in y_col.lower() or y_col == "active_after" else "#2ca02c",
        linewidth=1.6,
    )
    if shade_worst_sl:
        _shade_worst_sl(ax)
        ax.legend(loc="best")
    if log_y:
        ax.set_yscale("log")
    ax.set_title(title)
    ax.set_xlabel("Exit time (UTC)")
    ax.set_ylabel(ylabel)
    _style_time_axis(ax)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
    return out_path


def write_all_plots(
    paths_by_lev: dict[float, pd.DataFrame],
    out_dir: Path,
) -> dict[str, Path]:
    out: dict[str, Path] = {}
    out["active_equity_curve"] = plot_multi_leverage(
        paths_by_lev,
        y_col="active_after",
        title="Active Trading Equity",
        ylabel="ACTIVE (USDT)",
        out_path=out_dir / "active_equity_curve.png",
    )
    out["active_equity_curve_log"] = plot_multi_leverage(
        paths_by_lev,
        y_col="active_after",
        title="Active Trading Equity (log scale)",
        ylabel="ACTIVE (USDT, log)",
        out_path=out_dir / "active_equity_curve_log.png",
        log_y=True,
    )
    out["reserve_curve"] = plot_multi_leverage(
        paths_by_lev,
        y_col="reserve_after",
        title="Cashout / Loss Reserve",
        ylabel="RESERVE (USDT)",
        out_path=out_dir / "reserve_curve.png",
    )
    out["reserve_curve_log"] = plot_multi_leverage(
        paths_by_lev,
        y_col="reserve_after",
        title="Cashout / Loss Reserve (log scale)",
        ylabel="RESERVE (USDT, log)",
        out_path=out_dir / "reserve_curve_log.png",
        log_y=True,
    )
    out["total_wealth_curve"] = plot_multi_leverage(
        paths_by_lev,
        y_col="total_wealth_after",
        title="Total Wealth (ACTIVE + RESERVE) — secondary",
        ylabel="TOTAL WEALTH (USDT)",
        out_path=out_dir / "total_wealth_curve.png",
    )
    out["active_drawdown_curve"] = plot_multi_leverage(
        paths_by_lev,
        y_col="active_drawdown_pct",
        title="Active Equity Drawdown",
        ylabel="Active drawdown (%)",
        out_path=out_dir / "active_drawdown_curve.png",
        shade_worst_sl=True,
    )
    out["total_drawdown_curve"] = plot_multi_leverage(
        paths_by_lev,
        y_col="total_drawdown_pct",
        title="Total Wealth Drawdown",
        ylabel="Total drawdown (%)",
        out_path=out_dir / "total_drawdown_curve.png",
        shade_worst_sl=True,
    )

    p1 = paths_by_lev[1.0]
    out["active_equity_1x"] = plot_single_leverage(
        p1,
        y_col="active_after",
        title="Active Trading Equity — 1x",
        ylabel="ACTIVE (USDT)",
        out_path=out_dir / "active_equity_1x.png",
        shade_worst_sl=True,
    )
    out["reserve_1x"] = plot_single_leverage(
        p1,
        y_col="reserve_after",
        title="Cashout / Loss Reserve — 1x",
        ylabel="RESERVE (USDT)",
        out_path=out_dir / "reserve_1x.png",
        shade_worst_sl=True,
    )

    if 10.0 in paths_by_lev:
        p10 = paths_by_lev[10.0]
        dep = bool(p10["capital_depleted"].any()) if "capital_depleted" in p10.columns else False
        # still plot until depletion point
        out["active_equity_10x"] = plot_single_leverage(
            p10,
            y_col="active_after",
            title="Active Trading Equity — 10x"
            + (" (CAPITAL_DEPLETED)" if dep else ""),
            ylabel="ACTIVE (USDT)",
            out_path=out_dir / "active_equity_10x.png",
        )
        out["reserve_10x"] = plot_single_leverage(
            p10,
            y_col="reserve_after",
            title="Cashout / Loss Reserve — 10x"
            + (" (CAPITAL_DEPLETED)" if dep else ""),
            ylabel="RESERVE (USDT)",
            out_path=out_dir / "reserve_10x.png",
        )
    return out
