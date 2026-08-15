"""Reclaim confirmation after burst (close confirm → fill next open)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def reclaim_level(row: pd.Series, side: str, variant: str, prev_close: float | None) -> float | None:
    if variant == "R1":
        # event midpoint
        return (float(row["high"]) + float(row["low"])) / 2.0
    if variant == "R2":
        return float(row["open"])
    if variant == "R3":
        if prev_close is None or not np.isfinite(prev_close):
            return None
        return float(prev_close)
    return None


def check_reclaim(
    df: pd.DataFrame,
    *,
    anchor_i: int,
    side: str,
    variant: str,
    window: int,
) -> dict[str, Any] | None:
    """Return reclaim event if confirmed within window bars AFTER anchor.

    Confirmation: close crosses reclaim level on a bar strictly after anchor.
    Fill: next bar's open after confirmation close (no same-candle fill).
    """
    if anchor_i < 0 or anchor_i >= len(df) - 1:
        return None
    seq = int(df["sequence_id"].iloc[anchor_i])
    prev_close = float(df["close"].iloc[anchor_i - 1]) if anchor_i > 0 else None
    if anchor_i > 0 and int(df["sequence_id"].iloc[anchor_i - 1]) != seq:
        prev_close = None

    level = reclaim_level(df.iloc[anchor_i], side, variant, prev_close)
    if level is None or not np.isfinite(level):
        return None

    end = min(len(df) - 1, anchor_i + window)
    for j in range(anchor_i + 1, end + 1):
        if int(df["sequence_id"].iloc[j]) != seq:
            break
        dt = (df["bucket_start"].iloc[j] - df["bucket_start"].iloc[j - 1]).total_seconds()
        if dt != 300:
            break
        close = float(df["close"].iloc[j])
        ok = close > level if side == "long" else close < level
        if not ok:
            continue
        fill_i = j + 1
        if fill_i >= len(df):
            return None
        if int(df["sequence_id"].iloc[fill_i]) != seq:
            return None
        dt2 = (df["bucket_start"].iloc[fill_i] - df["bucket_start"].iloc[j]).total_seconds()
        if dt2 != 300:
            return None
        return {
            "reclaim_i": j,
            "fill_i": fill_i,
            "reclaim_level": level,
            "reclaim_variant": variant,
            "reclaim_window": window,
            "reclaim_bucket": str(df["bucket_start"].iloc[j]),
            "fill_bucket": str(df["bucket_start"].iloc[fill_i]),
            "fill_price": float(df["open"].iloc[fill_i]),
            "bars_to_reclaim": j - anchor_i,
        }
    return None
