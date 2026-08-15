"""First-valid / warm-up metadata from Frozen indicator columns. No formula change."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

FROZEN_INDICATOR_FIELDS = (
    "rsi",
    "stoch_k",
    "stoch_d",
    "cci",
    "ema9",
    "ema20",
    "ema100",
    "ema400",
)


def _iso(ts: Any) -> str | None:
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    else:
        t = t.tz_convert("UTC")
    return t.to_pydatetime().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _first_notna(df: pd.DataFrame, cols: tuple[str, ...], ts_col: str = "timestamp") -> Any:
    present = [c for c in cols if c in df.columns]
    if not present or df.empty:
        return None
    mask = df[present].notna().all(axis=1)
    if not mask.any():
        return None
    return df.loc[mask, ts_col].iloc[0]


def first_valid_from_indicators(
    ohlcv_ind: pd.DataFrame,
    *,
    signal_start: datetime,
) -> dict[str, Any]:
    if ohlcv_ind is None or ohlcv_ind.empty:
        return {
            "first_htf_bar_at": None,
            "first_indicator_valid_at": None,
            "first_ema400_valid_at": None,
            "first_stoch_valid_at": None,
            "htf_bar_count": 0,
            "htf_bar_count_before_signal_start": 0,
            "warmup_complete": False,
        }
    ts = pd.to_datetime(ohlcv_ind["timestamp"], utc=True)
    start = signal_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    before = int((ts < start).sum())
    first_htf = ts.iloc[0]
    first_ind = _first_notna(ohlcv_ind, FROZEN_INDICATOR_FIELDS)
    first_ema = _first_notna(ohlcv_ind, ("ema400",))
    first_stoch = _first_notna(ohlcv_ind, ("stoch_k", "stoch_d"))
    warmup = False
    if first_ind is not None:
        t = pd.Timestamp(first_ind)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        warmup = t.to_pydatetime().astimezone(timezone.utc) < start
    return {
        "first_htf_bar_at": _iso(first_htf),
        "first_indicator_valid_at": _iso(first_ind),
        "first_ema400_valid_at": _iso(first_ema),
        "first_stoch_valid_at": _iso(first_stoch),
        "htf_bar_count": int(len(ohlcv_ind)),
        "htf_bar_count_before_signal_start": before,
        "warmup_complete": warmup,
    }


def attach_confirmation_times(
    meta: dict[str, Any],
    events: pd.DataFrame | None,
) -> dict[str, Any]:
    out = dict(meta)
    out["first_candidate_confirmation_at"] = None
    out["first_tier_a_confirmation_at"] = None
    if events is None or events.empty or "confirmation_available_at" not in events.columns:
        return out
    conf = pd.to_datetime(events["confirmation_available_at"], utc=True)
    out["first_candidate_confirmation_at"] = _iso(conf.min())
    if "is_tier_a" in events.columns:
        ta = events.loc[events["is_tier_a"].astype(bool)]
        if not ta.empty:
            out["first_tier_a_confirmation_at"] = _iso(
                pd.to_datetime(ta["confirmation_available_at"], utc=True).min()
            )
    return out
