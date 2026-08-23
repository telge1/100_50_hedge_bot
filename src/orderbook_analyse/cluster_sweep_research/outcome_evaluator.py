"""Outcome evaluation for confirmation entry variants (label-only)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from .models import ConfirmationVariant, SetupDirection, SweepEvent


def evaluate_outcomes(
    event: SweepEvent,
    candles: pd.DataFrame,
    *,
    horizons_bars: tuple[int, ...] = (4, 8, 16, 32),
    tp_pcts: tuple[float, ...] = (0.005, 0.01, 0.02),
    sl_pcts: tuple[float, ...] = (0.005, 0.01),
    fee_bps: float = 2.0,
    slip_bps: float = 1.0,
) -> SweepEvent:
    """Fill event.outcomes per fired confirmation; costs applied to returns."""
    df = candles.sort_values("open_time").reset_index(drop=True).copy()
    df["open_time"] = pd.to_datetime(df["open_time"])
    cost = (fee_bps + slip_bps) / 10_000.0
    out: dict[str, Any] = {}

    for variant in ConfirmationVariant:
        info = event.confirmations.get(variant.value) or {}
        if not info.get("fired"):
            continue
        entry_t = event.t_earliest_entry
        if entry_t is None:
            out[variant.value] = {"status": "NO_ENTRY_TIME"}
            continue
        # find entry bar by open_time
        et = pd.Timestamp(entry_t).tz_localize(None) if pd.Timestamp(entry_t).tzinfo else pd.Timestamp(entry_t)
        hits = df.index[df["open_time"] == et]
        if len(hits) == 0:
            # nearest next
            later = df[df["open_time"] >= et]
            if later.empty:
                out[variant.value] = {"status": "OPEN_UNRESOLVED"}
                continue
            i = int(later.index[0])
            entry_px = float(later.iloc[0]["open"])
        else:
            i = int(hits[0])
            entry_px = float(df.iloc[i]["open"])

        side_sign = 1.0 if event.setup_direction == SetupDirection.BULLISH else -1.0
        path = df.iloc[i:]
        variant_out: dict[str, Any] = {
            "entry_time": str(df.iloc[i]["open_time"]),
            "entry_price": entry_px,
            "cost_frac": cost,
        }
        for h in horizons_bars:
            chunk = path.iloc[1 : h + 1]
            if chunk.empty:
                variant_out[f"h{h}"] = {"status": "OPEN_UNRESOLVED"}
                continue
            if event.setup_direction == SetupDirection.BULLISH:
                mfe = float(chunk["high"].max() / entry_px - 1.0)
                mae = float(1.0 - chunk["low"].min() / entry_px)
                ret = float(chunk.iloc[-1]["close"] / entry_px - 1.0) - cost
                t_mfe = int(chunk["high"].values.argmax()) + 1
                t_mae = int(chunk["low"].values.argmin()) + 1
            else:
                mfe = float(1.0 - chunk["low"].min() / entry_px)
                mae = float(chunk["high"].max() / entry_px - 1.0)
                ret = float(1.0 - chunk.iloc[-1]["close"] / entry_px) - cost
                t_mfe = int(chunk["low"].values.argmin()) + 1
                t_mae = int(chunk["high"].values.argmax()) + 1
            tp_sl = {}
            for tp in tp_pcts:
                for sl in sl_pcts:
                    tp_sl[f"tp{tp}_sl{sl}"] = _tp_sl_first(chunk, entry_px, event.setup_direction, tp, sl)
            variant_out[f"h{h}"] = {
                "mfe": mfe,
                "mae": mae,
                "return_net": ret,
                "t_mfe_bars": t_mfe,
                "t_mae_bars": t_mae,
                "tp_sl": tp_sl,
                "status": "RESOLVED",
            }
        out[variant.value] = variant_out

    event.outcomes = out
    return event


def _tp_sl_first(
    chunk: pd.DataFrame,
    entry: float,
    direction: SetupDirection,
    tp: float,
    sl: float,
) -> str:
    for _, r in chunk.iterrows():
        hi, lo = float(r["high"]), float(r["low"])
        if direction == SetupDirection.BULLISH:
            hit_tp = hi >= entry * (1 + tp)
            hit_sl = lo <= entry * (1 - sl)
        else:
            hit_tp = lo <= entry * (1 - tp)
            hit_sl = hi >= entry * (1 + sl)
        if hit_tp and hit_sl:
            return "BOTH_SAME_BAR"
        if hit_tp:
            return "TP_FIRST"
        if hit_sl:
            return "SL_FIRST"
    return "OPEN_UNRESOLVED"
