"""Common MySQL coverage window for DOGE + APT across required TFs."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_wave_fade_global_single_position_db import (
    COVERAGE_TFS,
    ENV_FILE,
    SYMBOLS,
)
from orderbook_analyse.trend_scanner_mysql_feather_parity.load import load_env_file


def _ts_utc(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def inventory_coverage(symbols: tuple[str, ...] = SYMBOLS) -> dict[str, Any]:
    load_env_file(ENV_FILE)
    rows: list[dict[str, Any]] = []
    per_sym: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        info: dict[str, Any] = {}
        for tf in COVERAGE_TFS:
            df = load_mysql_ohlcv_tf(symbol=sym, timeframe=tf, env_file=ENV_FILE)
            t0 = _ts_utc(df["timestamp"].min())
            t1 = _ts_utc(df["timestamp"].max())
            rec = {
                "symbol": sym,
                "timeframe": tf,
                "earliest": t0.isoformat(),
                "latest": t1.isoformat(),
                "n_bars": int(len(df)),
            }
            rows.append(rec)
            info[tf] = {"earliest": t0, "latest": t1, "n": int(len(df))}
        per_sym[sym] = info

    starts = [_ts_utc(per_sym[s][tf]["earliest"]) for s in symbols for tf in COVERAGE_TFS]
    ends = [_ts_utc(per_sym[s][tf]["latest"]) for s in symbols for tf in COVERAGE_TFS]
    common_start = max(starts)
    common_end = min(ends)
    complete = bool(common_start < common_end)
    return {
        "rows": rows,
        "per_symbol": per_sym,
        "common_start": common_start,
        "common_end": common_end,
        "complete": complete,
        "symbols": list(symbols),
        "timeframes": list(COVERAGE_TFS),
        "note": (
            "Common window = intersection of earliest/latest across DOGE+APT "
            "for 1m/15m/30m/1h/4h. No extrapolation."
        ),
    }


def coverage_frame(inv: dict[str, Any]) -> pd.DataFrame:
    df = pd.DataFrame(inv["rows"])
    df["common_start"] = inv["common_start"].isoformat()
    df["common_end"] = inv["common_end"].isoformat()
    df["common_complete"] = bool(inv["complete"])
    return df
