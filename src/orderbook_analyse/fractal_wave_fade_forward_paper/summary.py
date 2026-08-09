"""Summary metrics for paper trades (REPLAY vs TRUE_FORWARD separate)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _summarize(df: pd.DataFrame) -> dict[str, Any]:
    if df is None or df.empty:
        return {
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "expectancy": None,
            "profit_factor": None,
            "cumulative_net": 0.0,
            "max_drawdown": None,
            "win_rate": None,
        }
    nets = df["net_return_pct"].astype(float).to_numpy()
    wins = nets[nets > 0]
    losses = nets[nets < 0]
    eq = np.cumsum(nets)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    return {
        "closed_trades": int(len(nets)),
        "wins": int(len(wins)),
        "losses": int(len(losses)),
        "expectancy": float(np.mean(nets)),
        "profit_factor": (
            float(np.sum(wins) / abs(np.sum(losses)))
            if len(wins) and len(losses) and np.sum(losses) != 0
            else None
        ),
        "cumulative_net": float(np.sum(nets)),
        "max_drawdown": float(dd.min()) if len(dd) else None,
        "win_rate": float(np.mean(nets > 0)),
        "by_side": {
            side: {
                "n": int(len(g)),
                "expectancy": float(g["net_return_pct"].mean()) if len(g) else None,
            }
            for side, g in df.groupby("side")
        },
        "by_first_tf": {
            str(tf): int(len(g)) for tf, g in df.groupby("first_signal_tf")
        },
        "by_highest_tf": {
            str(tf): int(len(g)) for tf, g in df.groupby("highest_tf_reached")
        },
    }


def build_summary(
    trades: pd.DataFrame,
    state: dict[str, Any],
    *,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "strategy_version": state.get("strategy_version"),
        "paper_start": state.get("paper_start"),
        "runner_created_at": state.get("runner_created_at"),
        "forward_capture_start": state.get("forward_capture_start"),
        "mode_last_run": state.get("mode_last_run"),
        "parity_status": state.get("parity_status"),
        "fee_pct": state.get("fee_pct"),
        "conflict_exit_enabled": state.get("conflict_exit_enabled"),
        "symbols": {},
    }
    if trades is None or trades.empty:
        replay = true_fwd = trades
    else:
        replay = trades[trades["validation_mode"] == "REPLAY"]
        true_fwd = trades[trades["validation_mode"] == "TRUE_FORWARD"]

    out["REPLAY"] = _summarize(replay if isinstance(replay, pd.DataFrame) else pd.DataFrame())
    out["TRUE_FORWARD"] = _summarize(true_fwd if isinstance(true_fwd, pd.DataFrame) else pd.DataFrame())

    for sym, ss in (state.get("symbols") or {}).items():
        stdf = trades[trades["symbol"] == sym] if trades is not None and not trades.empty else pd.DataFrame()
        out["symbols"][sym] = {
            "status": ss.get("status"),
            "forward_coverage": ss.get("forward_coverage"),
            "last_processed_1m_ts": ss.get("last_processed_1m_ts"),
            "open_position": ss.get("open_position"),
            "n_entries": ss.get("n_entries"),
            "n_upgrades": ss.get("n_upgrades"),
            "n_closed": ss.get("n_closed"),
            "REPLAY": _summarize(stdf[stdf["validation_mode"] == "REPLAY"]) if not stdf.empty else _summarize(pd.DataFrame()),
            "TRUE_FORWARD": _summarize(stdf[stdf["validation_mode"] == "TRUE_FORWARD"]) if not stdf.empty else _summarize(pd.DataFrame()),
        }
    if extras:
        out.update(extras)
    return out
