"""Causal 4h level helpers for decisive-break (swing / range / lower-high)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _ts(x: Any) -> pd.Timestamp:
    t = pd.Timestamp(x)
    if t.tzinfo is None:
        return t.tz_localize("UTC")
    return t.tz_convert("UTC")


def prepare_h4_series(h4: pd.DataFrame) -> pd.DataFrame:
    out = h4.copy().reset_index(drop=True)
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True)
    if "htf_close_decision" not in out.columns:
        out["htf_close_decision"] = out["timestamp"] + pd.Timedelta(hours=4)
    else:
        out["htf_close_decision"] = pd.to_datetime(out["htf_close_decision"], utc=True)
    for col in ("open", "high", "low", "close"):
        out[col] = out[col].astype(float)
    return out


def confirmed_swing_lows(h4: pd.DataFrame) -> list[dict[str, Any]]:
    """Pivot low at i confirmed when bar i+1 is closed: low[i] < low[i-1] and low[i] <= low[i+1]."""
    lows = h4["low"].to_numpy(dtype=float)
    out: list[dict[str, Any]] = []
    for i in range(1, len(h4) - 1):
        if lows[i] < lows[i - 1] and lows[i] <= lows[i + 1]:
            out.append(
                {
                    "index": i,
                    "price": float(lows[i]),
                    "formed_ts": str(h4.iloc[i]["timestamp"]),
                    "confirmed_ts": str(h4.iloc[i + 1]["htf_close_decision"]),
                    "confirm_index": i + 1,
                }
            )
    return out


def confirmed_swing_highs(h4: pd.DataFrame) -> list[dict[str, Any]]:
    highs = h4["high"].to_numpy(dtype=float)
    out: list[dict[str, Any]] = []
    for i in range(1, len(h4) - 1):
        if highs[i] > highs[i - 1] and highs[i] >= highs[i + 1]:
            out.append(
                {
                    "index": i,
                    "price": float(highs[i]),
                    "formed_ts": str(h4.iloc[i]["timestamp"]),
                    "confirmed_ts": str(h4.iloc[i + 1]["htf_close_decision"]),
                    "confirm_index": i + 1,
                }
            )
    return out


def latest_lower_high(
    swings_high: list[dict[str, Any]],
    *,
    asof_idx: int,
    arm_idx: int,
) -> dict[str, Any] | None:
    """Most recent confirmed lower high with confirm_index <= asof_idx and form after arm."""
    visible = [s for s in swings_high if s["confirm_index"] <= asof_idx and s["index"] >= arm_idx]
    if len(visible) < 2:
        return None
    # find last pair where price[k] < price[k-1]
    for k in range(len(visible) - 1, 0, -1):
        if visible[k]["price"] < visible[k - 1]["price"]:
            return {**visible[k], "prior_high": visible[k - 1]["price"]}
    return None


def range_support_level(
    h4: pd.DataFrame,
    *,
    asof_idx: int,
    start_idx: int,
    lookback: int,
) -> dict[str, Any] | None:
    """Causal range support = min low of last `lookback` completed bars ending at asof_idx.

    Ready only when lookback bars are available after start_idx and the range has
    at least one bounce (current low is not strictly the sole new extreme without prior bars).
    """
    if asof_idx < 0 or lookback < 2:
        return None
    lo = max(start_idx, asof_idx - lookback + 1)
    if asof_idx - lo + 1 < lookback:
        return None
    window = h4.iloc[lo : asof_idx + 1]
    level = float(window["low"].min())
    # require the min not only on the last bar (some holding above support)
    last_low = float(h4.iloc[asof_idx]["low"])
    if last_low < level - 1e-12:
        return None
    # bounce confirmation: at least one prior bar in window had low > level * (1+eps) or close > level
    holds = (window["close"] >= level).sum()
    if int(holds) < max(2, lookback // 3):
        return None
    return {
        "price": level,
        "formed_ts": str(window.loc[window["low"].idxmin(), "timestamp"]),
        "confirmed_ts": str(h4.iloc[asof_idx]["htf_close_decision"]),
        "source": f"range_min_low_last_{lookback}",
        "lookback": lookback,
        "start_index": lo,
        "end_index": asof_idx,
    }


def pick_decisive_level(
    h4: pd.DataFrame,
    *,
    asof_idx: int,
    arm_idx: int,
    stabilize_bars: int,
    range_lookback: int,
    swings_low: list[dict[str, Any]],
    swings_high: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return (level_dict, lower_high_dict_or_none). Level only after stabilize_bars post-arm."""
    if asof_idx < 0 or arm_idx is None:
        return None, None
    # bars fully closed after arm bar
    bars_after = asof_idx - arm_idx
    if bars_after < stabilize_bars:
        return None, None

    lh = latest_lower_high(swings_high, asof_idx=asof_idx, arm_idx=arm_idx)

    # D1: most recent confirmed swing low formed at/after arm, confirmed by asof
    cands = [
        s
        for s in swings_low
        if s["index"] >= arm_idx and s["confirm_index"] <= asof_idx and s["confirm_index"] > arm_idx
    ]
    if cands:
        s = cands[-1]
        return (
            {
                "value": float(s["price"]),
                "level_type": "confirmed_swing_low_4h",
                "source": "D1_confirmed_swing_low",
                "formed_ts": s["formed_ts"],
                "confirmed_ts": s["confirmed_ts"],
                "lower_high_ts": None if lh is None else lh["confirmed_ts"],
            },
            lh,
        )

    # D2 fallback: range support after stabilize
    rng = range_support_level(
        h4, asof_idx=asof_idx, start_idx=arm_idx + 1, lookback=range_lookback
    )
    if rng is not None:
        return (
            {
                "value": float(rng["price"]),
                "level_type": "range_support_4h",
                "source": "D2_range_support",
                "formed_ts": rng["formed_ts"],
                "confirmed_ts": rng["confirmed_ts"],
                "lower_high_ts": None if lh is None else lh["confirmed_ts"],
            },
            lh,
        )
    return None, lh
