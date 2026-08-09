"""MySQL coverage inventory for walk-forward validation."""

from __future__ import annotations

from typing import Any

import pandas as pd

from orderbook_analyse.fractal_cycle_wave_analysis.load_mysql import load_mysql_ohlcv_tf
from orderbook_analyse.fractal_wave_fade_walkforward_validation_db import ENV_FILE, SYMBOLS


COVERAGE_TFS = ("1m", "5m", "15m", "30m", "1h", "4h")


def inventory_coverage() -> dict[str, Any]:
    rows = []
    per_sym = {}
    for sym in SYMBOLS:
        info = {}
        for tf in COVERAGE_TFS:
            df = load_mysql_ohlcv_tf(symbol=sym, timeframe=tf, env_file=ENV_FILE)
            t0 = pd.Timestamp(df["timestamp"].min())
            t1 = pd.Timestamp(df["timestamp"].max())
            if t0.tzinfo is None:
                t0 = t0.tz_localize("UTC")
            else:
                t0 = t0.tz_convert("UTC")
            if t1.tzinfo is None:
                t1 = t1.tz_localize("UTC")
            else:
                t1 = t1.tz_convert("UTC")
            rec = {
                "symbol": sym,
                "timeframe": tf,
                "earliest": t0.isoformat(),
                "latest": t1.isoformat(),
                "n_bars": int(len(df)),
            }
            rows.append(rec)
            info[tf] = {"earliest": t0, "latest": t1, "n": int(len(df))}
        # testable window = 1m execution coverage
        test_start = info["1m"]["earliest"]
        test_end = info["1m"]["latest"]
        per_sym[sym] = {
            "tfs": info,
            "testable_start": test_start,
            "testable_end": test_end,
            "note": (
                "BTC 1m ends far earlier than HTF — strategy execution limited by 1m."
                if sym == "BTCUSDT"
                else "DOGE 1m spans full HTF overlap."
            ),
        }
    return {"rows": rows, "per_symbol": per_sym}
