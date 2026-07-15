"""Read-only window characterization from candle data (no variant results)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from research.regime_scanner.data_loader import load_symbol_candles
from research.regime_scanner.timeframes import ensure_utc_timestamp


def characterize_candle_window(
    frame: pd.DataFrame,
    *,
    start: object,
    end: object,
) -> dict[str, Any]:
    """Price/volatility evidence for a candidate analysis window [start, end)."""
    start_ts = ensure_utc_timestamp(start)
    end_ts = ensure_utc_timestamp(end)
    w = frame.loc[(frame["timestamp"] >= start_ts) & (frame["timestamp"] < end_ts)].copy()
    if w.empty:
        return {"error": "empty_window", "bars": 0}
    open_px = float(w["open"].iloc[0])
    close_px = float(w["close"].iloc[-1])
    ret_pct = (close_px / open_px - 1.0) * 100.0 if open_px else 0.0
    high = float(w["high"].max())
    low = float(w["low"].min())
    range_pct = (high / low - 1.0) * 100.0 if low else 0.0
    vol_pct = float(w["close"].pct_change().std() * 100.0)
    moves = float(w["close"].diff().abs().sum())
    net = abs(close_px - open_px)
    trendiness = float(net / moves) if moves else 0.0
    return {
        "bars": int(len(w)),
        "open": open_px,
        "close": close_px,
        "return_pct": float(f"{ret_pct:.6g}"),
        "range_pct": float(f"{range_pct:.6g}"),
        "volatility_pct": float(f"{vol_pct:.6g}"),
        "trendiness": float(f"{trendiness:.6g}"),
        "start": start_ts.isoformat(),
        "end": end_ts.isoformat(),
    }


def load_apt_5m(*, data_source: str = "mysql") -> pd.DataFrame:
    df = load_symbol_candles("APTUSDT", data_source=data_source)  # type: ignore[arg-type]
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df.sort_values("timestamp").reset_index(drop=True)


def build_window_evidence(
    *,
    start: str,
    end: str,
    data_source: str = "mysql",
) -> dict[str, Any]:
    frame = load_apt_5m(data_source=data_source)
    return characterize_candle_window(frame, start=start, end=end)
