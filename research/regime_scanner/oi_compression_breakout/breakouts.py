"""Close-confirmed breakouts after frozen box confirmation.

Breakout-probability window (W3…W48) is independent of trading MFE horizons.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from research.regime_scanner.oi_compression_breakout.boxes import FrozenBox
from research.regime_scanner.oi_compression_breakout.config import MAX_WAIT_BARS, WAIT_WINDOWS
from research.regime_scanner.oi_compression_breakout.features import contiguous_same_sequence


def scan_breakout(
    df: pd.DataFrame,
    box: FrozenBox,
    *,
    max_wait: int = MAX_WAIT_BARS,
) -> dict[str, Any]:
    """Observe bars confirm+1 … confirm+max_wait for close outside frozen box.

    Does not mutate box membership. Timeout / gap / dataset-end are first-class
    outcomes with ``no_breakout=true`` (except breakout_no_fill).

    For production, ``max_wait`` must be >= max(WAIT_WINDOWS) so W24/W48 are
    observed; shorter values are allowed in unit tests and leave Wx as False
    when the horizon was not fully observed and no breakout occurred.
    """
    highs = df["high"].to_numpy(dtype=float)
    lows = df["low"].to_numpy(dtype=float)
    closes = df["close"].to_numpy(dtype=float)
    opens = df["open"].to_numpy(dtype=float)
    seq = df["sequence_id"].to_numpy()
    ts = df["bucket_start"].to_numpy()
    n = len(df)

    result: dict[str, Any] = {
        "box_id": box.box_id,
        "physical_id": box.physical_id,
        "symbol": box.symbol,
        "box_length": box.box_length,
        "quality": box.quality,
        "box_high": box.box_high,
        "box_low": box.box_low,
        "confirm_i": box.confirm_i,
        "breakout_side": None,
        "breakout_i": None,
        "bars_to_breakout": None,
        "fill_i": None,
        "fill_price": None,
        "fill_bucket": None,
        "breakout_bucket": None,
        "invalidated": False,
        "invalidation_reason": None,
        "status": "pending",
        "outcome_status": "pending",
        "no_breakout": True,
        "search_horizon_bars": int(max_wait),
        "observed_search_bars": 0,
    }

    first_long = None
    first_short = None
    planned_end = box.confirm_i + max_wait
    end = min(n - 1, planned_end)
    observed_bars = 0
    stop_reason = None

    for j in range(box.confirm_i + 1, end + 1):
        if not contiguous_same_sequence(seq, ts, box.confirm_i, j):
            result["invalidated"] = True
            if j > 0 and seq[j] != seq[j - 1]:
                result["outcome_status"] = "sequence_end"
                result["invalidation_reason"] = "sequence_end"
                result["status"] = "sequence_end"
            else:
                result["outcome_status"] = "gap_abort"
                result["invalidation_reason"] = "gap_abort"
                result["status"] = "gap_abort"
            stop_reason = "gap"
            break
        observed_bars += 1
        if closes[j] > box.box_high and first_long is None:
            first_long = j
        if closes[j] < box.box_low and first_short is None:
            first_short = j
        if first_long is not None or first_short is not None:
            stop_reason = "breakout"
            break
    else:
        if end < planned_end:
            stop_reason = "dataset_end"
        else:
            stop_reason = "timeout"

    result["observed_search_bars"] = int(observed_bars)

    for w in WAIT_WINDOWS:
        key = f"W{w}"
        if first_long is None and first_short is None and w > max_wait:
            # horizon not fully observed — conservative none
            result[f"{key}_long"] = False
            result[f"{key}_short"] = False
            result[f"{key}_any"] = False
            result[f"{key}_both"] = False
            result[f"{key}_none"] = True
            result[f"{key}_horizon_observed"] = False
            continue
        long_by = first_long is not None and (first_long - box.confirm_i) <= w
        short_by = first_short is not None and (first_short - box.confirm_i) <= w
        result[f"{key}_long"] = bool(long_by)
        result[f"{key}_short"] = bool(short_by)
        result[f"{key}_any"] = bool(long_by or short_by)
        result[f"{key}_both"] = bool(long_by and short_by)
        result[f"{key}_none"] = not (long_by or short_by)
        result[f"{key}_horizon_observed"] = True

    if stop_reason in ("gap",) and first_long is None and first_short is None:
        result["no_breakout"] = True
        result["status"] = result["outcome_status"]
        return result

    if first_long is None and first_short is None:
        result["no_breakout"] = True
        result["breakout_side"] = None
        result["breakout_i"] = None
        result["fill_i"] = None
        result["fill_price"] = None
        result["invalidated"] = False
        result["invalidation_reason"] = None
        if stop_reason == "dataset_end":
            result["status"] = "dataset_end"
            result["outcome_status"] = "dataset_end"
        else:
            result["status"] = "no_breakout_timeout"
            result["outcome_status"] = "no_breakout_timeout"
        return result

    if first_long is not None and (first_short is None or first_long <= first_short):
        bi, side = first_long, "long"
    else:
        bi, side = first_short, "short"

    fill_i = bi + 1
    result["no_breakout"] = False
    result["breakout_side"] = side
    result["breakout_i"] = bi
    result["bars_to_breakout"] = int(bi - box.confirm_i)
    result["breakout_bucket"] = str(df["bucket_start"].iloc[bi])
    result["breakout_close"] = float(closes[bi])
    result["breakout_distance"] = (
        float(closes[bi] - box.box_high) if side == "long" else float(box.box_low - closes[bi])
    )
    result["outcome_status"] = f"breakout_{side}"

    if fill_i >= n or not contiguous_same_sequence(seq, ts, bi, fill_i):
        result["invalidated"] = True
        result["invalidation_reason"] = "no_next_open_fill"
        result["status"] = "breakout_no_fill"
        result["outcome_status"] = "breakout_no_fill"
        result["fill_i"] = None
        result["fill_price"] = None
        result["fill_bucket"] = None
        return result

    rng = highs[bi] - lows[bi]
    atr = float(df["atr_14"].iloc[bi]) if "atr_14" in df.columns else np.nan
    loc = (closes[bi] - lows[bi]) / rng if rng > 0 else np.nan
    e1 = bool(np.isfinite(atr) and atr > 0 and (rng / atr) >= 1.0)
    e2 = bool((side == "long" and loc >= 0.75) or (side == "short" and loc <= 0.25))

    result.update(
        {
            "status": "breakout",
            "fill_i": fill_i,
            "fill_price": float(opens[fill_i]),
            "fill_bucket": str(df["bucket_start"].iloc[fill_i]),
            "E0": True,
            "E1": e1,
            "E2": e2,
            "E3": bool(e1 and e2),
            "same_candle_fill": False,
        }
    )
    return result
