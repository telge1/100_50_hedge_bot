"""Causal path price helpers used by LF1 outcomes.

LF1 only needs ``choose_reference`` and ``path_arrays_from_trades``.
This module intentionally omits high-conviction batch loaders so the
worktree package does not require ``high_conviction_aligned_filter_v1`` /
``continuation_batch_v1`` merely to import outcomes.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from ..outcomes.prices import CausalPriceIndex
from ..outcomes.public_trade_index import PublicTradeIndex
from . import MAX_HORIZON_S


def choose_reference(
    *,
    detection: pd.Timestamp,
    mid_index: CausalPriceIndex | None,
    trades: PublicTradeIndex | None,
    mid_max_age_ms: int = 1500,
) -> dict[str, Any]:
    """Causal reference at detection: prefer stored 1s mid, else first trade at/after detection."""
    detection = pd.Timestamp(detection).tz_convert("UTC")
    if mid_index is not None:
        pt = mid_index.latest_at_or_before(detection, max_age_ms=mid_max_age_ms, require_valid=True)
        if pt is not None and pt.available_at <= detection:
            return {
                "ok": True,
                "reference_price": float(pt.mid),
                "reference_price_ts": pt.available_at,
                "price_source": "MID_1S_AT_DETECTION",
                "selection": (
                    "Causal 1s book mid with available_at=state_ts+1s, "
                    "latest available_at <= detection_available_at, age<=1500ms."
                ),
                "state_ts": pt.state_ts,
            }
    if trades is not None and trades.trades:
        # First trade at or after detection (no lookback, no future cherry-pick).
        tval = int(detection.value)
        lo = int(np.searchsorted(trades._ts, tval, side="left"))
        if lo < len(trades.trades):
            tr = trades.trades[lo]
            return {
                "ok": True,
                "reference_price": float(tr.price),
                "reference_price_ts": tr.trade_ts,
                "price_source": "FIRST_PUBLIC_TRADE_AT_OR_AFTER_DETECTION",
                "selection": (
                    "No causal 1s mid within 1500ms at detection; "
                    "used first public trade with trade_ts >= detection_available_at."
                ),
                "trade_id": tr.trade_id,
            }
    return {
        "ok": False,
        "reference_price": None,
        "reference_price_ts": None,
        "price_source": None,
        "selection": "NO_CAUSAL_REFERENCE_PRICE",
    }


def path_arrays_from_trades(
    trades: PublicTradeIndex,
    *,
    detection: pd.Timestamp,
    horizon_s: int = MAX_HORIZON_S,
) -> tuple[np.ndarray, np.ndarray]:
    detection = pd.Timestamp(detection).tz_convert("UTC")
    end = detection + pd.Timedelta(seconds=int(horizon_s))
    sl = trades.path_slice(detection, end)
    if not sl:
        return np.array([], dtype="datetime64[ns]"), np.array([], dtype=float)
    ts = np.array([t.trade_ts.to_datetime64() for t in sl])
    px = np.array([float(t.price) for t in sl], dtype=float)
    return ts, px


def path_arrays_from_mid(
    mid_index: CausalPriceIndex,
    *,
    detection: pd.Timestamp,
    horizon_s: int = MAX_HORIZON_S,
) -> tuple[np.ndarray, np.ndarray]:
    detection = pd.Timestamp(detection).tz_convert("UTC")
    end = detection + pd.Timedelta(seconds=int(horizon_s))
    ts_list: list[pd.Timestamp] = []
    px_list: list[float] = []
    for p in mid_index.points:
        if p.available_at < detection or p.available_at > end:
            continue
        if p.price_valid != 1:
            continue
        ts_list.append(p.available_at)
        px_list.append(float(p.mid))
    if not ts_list:
        return np.array([], dtype="datetime64[ns]"), np.array([], dtype=float)
    return np.array([t.to_datetime64() for t in ts_list]), np.array(px_list, dtype=float)
