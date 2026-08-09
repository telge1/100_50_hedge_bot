"""Causal MFE/MAE from MySQL 1m path between entry and exit."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_dynamic_cluster_upgrade_db.simulate import (
    _mfe_mae_slice,
    tpsl_for_tf,
)
from orderbook_analyse.fractal_signal_confluence_db import ENV_FILE
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def load_1m_books(symbols: tuple[str, ...]) -> dict[str, dict[str, np.ndarray]]:
    load_env_file(ENV_FILE)
    books: dict[str, dict[str, np.ndarray]] = {}
    for sym in symbols:
        print(f"[mfe] load {sym} 1m …", flush=True)
        c = load_mysql_ohlcv_tf(symbol=sym, timeframe="1m", env_file=ENV_FILE)
        books[sym] = {
            "open_times": c["timestamp"].to_numpy(dtype="datetime64[ns]"),
            "high": c["high"].astype(float).to_numpy(),
            "low": c["low"].astype(float).to_numpy(),
        }
    return books


def annotate_mfe_mae(trades: pd.DataFrame, books: dict[str, dict[str, np.ndarray]]) -> pd.DataFrame:
    out = trades.copy()
    mfes, maes, tp_tgts, ratios = [], [], [], []
    for _, tr in out.iterrows():
        sym = str(tr["symbol"])
        b = books[sym]
        et = np.datetime64(
            pd.Timestamp(tr["entry_time"]).tz_convert("UTC").tz_localize(None).to_datetime64()
        )
        xt = np.datetime64(
            pd.Timestamp(tr["exit_time"]).tz_convert("UTC").tz_localize(None).to_datetime64()
        )
        ei = int(np.searchsorted(b["open_times"], et, side="left"))
        xi = int(np.searchsorted(b["open_times"], xt, side="left"))
        if (
            ei >= len(b["open_times"])
            or xi >= len(b["open_times"])
            or b["open_times"][ei] != et
            or b["open_times"][xi] != xt
        ):
            mfes.append(np.nan)
            maes.append(np.nan)
            tp_tgts.append(np.nan)
            ratios.append(np.nan)
            continue
        mfe, mae = _mfe_mae_slice(
            str(tr["side"]),
            float(tr["entry_price"]),
            b["high"],
            b["low"],
            ei,
            xi,
        )
        tp_pct, _ = tpsl_for_tf(str(tr["highest_tf_reached"]), extra_4h=False)
        mfes.append(mfe)
        maes.append(mae)
        tp_tgts.append(tp_pct)
        ratios.append(mfe / tp_pct if tp_pct > 0 else np.nan)
    out["mfe_pct"] = mfes
    out["mae_pct"] = maes
    out["tp_target_pct"] = tp_tgts
    out["mfe_over_tp"] = ratios
    return out


def mfe_by_period(trades_mfe: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for period, g in trades_mfe.groupby("period", sort=True):
        mfe = g["mfe_pct"].astype(float)
        mae = g["mae_pct"].astype(float)
        ratio = g["mfe_over_tp"].astype(float)
        rows.append(
            {
                "period": period,
                "n": int(len(g)),
                "median_mfe_pct": float(mfe.median()) if mfe.notna().any() else None,
                "mean_mfe_pct": float(mfe.mean()) if mfe.notna().any() else None,
                "median_mae_pct": float(mae.median()) if mae.notna().any() else None,
                "mean_mae_pct": float(mae.mean()) if mae.notna().any() else None,
                "median_mfe_over_tp": float(ratio.median()) if ratio.notna().any() else None,
                "mean_mfe_over_tp": float(ratio.mean()) if ratio.notna().any() else None,
                "share_mfe_ge_tp": float((ratio >= 1.0).mean()) if ratio.notna().any() else None,
            }
        )
    return pd.DataFrame(rows)
