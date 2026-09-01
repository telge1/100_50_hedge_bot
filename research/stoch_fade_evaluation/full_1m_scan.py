"""NO_BE50 full-1m TP/SL scan for Frozen research evaluation.

Does not call hold_end_i / STRATEGY_MAX_HOLD. SG time-exit path stays untouched.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from .config import iso_z


def parse_utc(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.to_pydatetime()


def truncate_to_pin(frame: pd.DataFrame, candle_data_to: datetime | None) -> pd.DataFrame:
    if frame is None or frame.empty:
        return frame
    out = frame.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    out = out.sort_values("timestamp").reset_index(drop=True)
    if candle_data_to is None:
        return out
    pin = pd.Timestamp(candle_data_to)
    if pin.tzinfo is None:
        pin = pin.tz_localize("UTC")
    else:
        pin = pin.tz_convert("UTC")
    return out.loc[out["timestamp"] <= pin].reset_index(drop=True)


def scan_first_barrier_sl_first(
    *,
    side: str,
    high: np.ndarray,
    low: np.ndarray,
    start_i: int,
    end_i: int,
    tp_price: float,
    sl_price: float,
) -> tuple[str | None, int | None, bool]:
    if end_i < start_i:
        return None, None, False
    hh = high[start_i : end_i + 1]
    ll = low[start_i : end_i + 1]
    if hh.size == 0:
        return None, None, False
    side_u = str(side).upper()
    if side_u == "LONG":
        hit_tp = hh >= tp_price
        hit_sl = ll <= sl_price
    else:
        hit_tp = ll <= tp_price
        hit_sl = hh >= sl_price
    any_tp = bool(np.any(hit_tp))
    any_sl = bool(np.any(hit_sl))
    i_tp = int(np.argmax(hit_tp)) if any_tp else -1
    i_sl = int(np.argmax(hit_sl)) if any_sl else -1
    if not any_tp and not any_sl:
        return None, None, False
    if not any_tp or (any_sl and i_sl <= i_tp):
        amb = bool(any_tp and any_sl and i_sl == i_tp)
        return "SL", start_i + i_sl, amb
    return "TP", start_i + i_tp, False


def _pnl_pct(side: str, entry: float, exit_px: float) -> float:
    if str(side).upper() == "LONG":
        return (exit_px / entry - 1.0) * 100.0
    return (entry - exit_px) / entry * 100.0


def evaluate_signal_no_be50_full_1m(
    signal: dict[str, Any],
    c1m: pd.DataFrame,
    *,
    candle_data_to: datetime | None = None,
) -> dict[str, Any]:
    """WIN/LOSS on first 1m TP/SL touch; OPEN only at pinned history end."""
    side = str(signal.get("direction") or "").upper()
    entry_time = parse_utc(signal.get("entry_time"))
    entry_price = signal.get("entry_price")
    tp_price = signal.get("tp_price")
    sl_price = signal.get("sl_price") if signal.get("sl_price") is not None else signal.get("initial_sl_price")
    try:
        ep = float(entry_price)
        tp = float(tp_price)
        sl = float(sl_price)
    except (TypeError, ValueError):
        ep = tp = sl = None

    base = {
        "result": "OPEN",
        "display_result": "OPEN",
        "entry_time": iso_z(entry_time) if entry_time else None,
        "entry_price": ep,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "pnl_pct": None,
        "duration_seconds": None,
        "ambiguity_flag": "",
        "be50_activated": False,
        "max_hold_applied": False,
    }

    if ep is None or ep <= 0 or tp is None or sl is None:
        base["ambiguity_flag"] = "INVALID_LEVELS"
        return base

    df = truncate_to_pin(c1m, candle_data_to)
    if df is None or df.empty:
        base["ambiguity_flag"] = "NO_CANDLES"
        base["exit_reason"] = "END_OF_HISTORY"
        return base

    ts = pd.to_datetime(df["timestamp"], utc=True)
    last_ts = ts.iloc[-1].to_pydatetime()
    pin_end = candle_data_to or last_ts
    if entry_time is None:
        base["ambiguity_flag"] = "INVALID_TIMESTAMP"
        return base

    et = pd.Timestamp(entry_time)
    if et.tzinfo is None:
        et = et.tz_localize("UTC")
    else:
        et = et.tz_convert("UTC")
    mask = ts >= et
    if not bool(mask.any()):
        base["ambiguity_flag"] = "NO_CANDLES_AFTER_ENTRY"
        base["exit_reason"] = "END_OF_HISTORY"
        base["duration_seconds"] = max(0, int((pin_end - entry_time).total_seconds()))
        return base

    start_i = int(np.flatnonzero(mask.to_numpy())[0])
    if pd.Timestamp(ts.iloc[start_i]) != et:
        base["ambiguity_flag"] = "ENTRY_BAR_MISSING"
        return base

    high = df["high"].astype(float).to_numpy()
    low = df["low"].astype(float).to_numpy()
    end_i = len(df) - 1
    kind, exit_i, amb = scan_first_barrier_sl_first(
        side=side, high=high, low=low, start_i=start_i, end_i=end_i, tp_price=tp, sl_price=sl
    )
    duration_open = max(0, int((pin_end - entry_time).total_seconds()))
    if kind is None:
        base["exit_reason"] = "END_OF_HISTORY"
        base["duration_seconds"] = duration_open
        base["ambiguity_flag"] = "AMBIGUOUS_INTRABAR" if amb else ""
        return base

    exit_ts = ts.iloc[int(exit_i)].to_pydatetime()
    exit_px = sl if kind == "SL" else tp
    result = "LOSS" if kind == "SL" else "WIN"
    base.update(
        {
            "result": result,
            "display_result": result,
            "exit_time": iso_z(exit_ts),
            "exit_price": float(exit_px),
            "exit_reason": kind,
            "pnl_pct": _pnl_pct(side, ep, float(exit_px)),
            "duration_seconds": int((exit_ts - entry_time).total_seconds()),
            "ambiguity_flag": "AMBIGUOUS_INTRABAR" if amb else "",
        }
    )
    return base
