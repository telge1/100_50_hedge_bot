"""Causal confirmed pivot highs / lows for the regime scanner."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

from .config import RegimeScannerConfig, default_regime_scanner_config

PivotType = Literal["high", "low"]


@dataclass(frozen=True)
class ConfirmedPivot:
    pivot_index: int
    pivot_timestamp: str
    confirmation_index: int
    confirmation_timestamp: str
    price: float
    pivot_type: PivotType

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ts_iso(value: object) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.isoformat()


def find_confirmed_pivots(
    candles: pd.DataFrame,
    *,
    config: RegimeScannerConfig | None = None,
    pivot_left: int | None = None,
    pivot_right: int | None = None,
) -> list[ConfirmedPivot]:
    """Return pivots that are already confirmed within ``candles``.

    A pivot at index ``i`` becomes known only on candle ``i + pivot_right``.
    Equal highs/lows do **not** form a pivot (strict inequality).
    """
    cfg = config or default_regime_scanner_config()
    left = int(cfg.pivot_left if pivot_left is None else pivot_left)
    right = int(cfg.pivot_right if pivot_right is None else pivot_right)
    if left < 0 or right < 0:
        raise ValueError("pivot_left/pivot_right must be non-negative")
    if candles.empty:
        return []
    required = ("timestamp", "high", "low")
    missing = [c for c in required if c not in candles.columns]
    if missing:
        raise ValueError(f"candles missing columns for pivots: {missing}")

    highs = pd.to_numeric(candles["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(candles["low"], errors="coerce").to_numpy(dtype=float)
    timestamps = list(candles["timestamp"])
    n = len(candles)
    pivots: list[ConfirmedPivot] = []

    # Last index that can serve as confirmation for some pivot i is n-1,
    # so i_max = n - 1 - right.
    last_confirmable = n - 1 - right
    if last_confirmable < left:
        return []

    for i in range(left, last_confirmable + 1):
        h = highs[i]
        l = lows[i]
        if not np.isfinite(h) or not np.isfinite(l):
            continue

        left_highs = highs[i - left : i]
        right_highs = highs[i + 1 : i + 1 + right]
        if (
            left_highs.size == left
            and right_highs.size == right
            and np.all(np.isfinite(left_highs))
            and np.all(np.isfinite(right_highs))
            and bool(np.all(h > left_highs))
            and bool(np.all(h > right_highs))
        ):
            confirm_i = i + right
            pivots.append(
                ConfirmedPivot(
                    pivot_index=i,
                    pivot_timestamp=_ts_iso(timestamps[i]),
                    confirmation_index=confirm_i,
                    confirmation_timestamp=_ts_iso(timestamps[confirm_i]),
                    price=float(h),
                    pivot_type="high",
                )
            )

        left_lows = lows[i - left : i]
        right_lows = lows[i + 1 : i + 1 + right]
        if (
            left_lows.size == left
            and right_lows.size == right
            and np.all(np.isfinite(left_lows))
            and np.all(np.isfinite(right_lows))
            and bool(np.all(l < left_lows))
            and bool(np.all(l < right_lows))
        ):
            confirm_i = i + right
            pivots.append(
                ConfirmedPivot(
                    pivot_index=i,
                    pivot_timestamp=_ts_iso(timestamps[i]),
                    confirmation_index=confirm_i,
                    confirmation_timestamp=_ts_iso(timestamps[confirm_i]),
                    price=float(l),
                    pivot_type="low",
                )
            )

    pivots.sort(key=lambda p: (p.confirmation_index, p.pivot_index, p.pivot_type))
    return pivots


def filter_pivots_as_of(
    pivots: list[ConfirmedPivot],
    decision_time: pd.Timestamp | str,
) -> list[ConfirmedPivot]:
    """Keep pivots whose confirmation timestamp is strictly before decision_time."""
    decision_ts = pd.Timestamp(decision_time)
    if decision_ts.tzinfo is None:
        decision_ts = decision_ts.tz_localize("UTC")
    else:
        decision_ts = decision_ts.tz_convert("UTC")
    kept: list[ConfirmedPivot] = []
    for pivot in pivots:
        conf = pd.Timestamp(pivot.confirmation_timestamp)
        if conf.tzinfo is None:
            conf = conf.tz_localize("UTC")
        else:
            conf = conf.tz_convert("UTC")
        if conf < decision_ts:
            kept.append(pivot)
    return kept


def pivots_by_type(
    pivots: list[ConfirmedPivot],
    pivot_type: PivotType,
) -> list[ConfirmedPivot]:
    return [p for p in pivots if p.pivot_type == pivot_type]


def latest_pivots(
    pivots: list[ConfirmedPivot],
    pivot_type: PivotType,
    *,
    count: int = 2,
) -> list[ConfirmedPivot]:
    selected = pivots_by_type(pivots, pivot_type)
    if count <= 0:
        return []
    return selected[-count:]


_INDICATOR_FIELDS = ("atr", "atr_pct", "adx", "plus_di", "minus_di", "di_spread")


def _finite_cell(frame: pd.DataFrame, index: int, column: str) -> float | None:
    if column not in frame.columns or index < 0 or index >= len(frame):
        return None
    try:
        number = float(frame.iloc[index][column])
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number


def enrich_pivot(
    pivot: ConfirmedPivot,
    frame: pd.DataFrame,
    *,
    timeframe: str,
) -> dict[str, Any]:
    """Attach timeframe + indicator values measured at the pivot bar."""
    payload = pivot.to_dict()
    payload["timeframe"] = str(timeframe)
    for field in _INDICATOR_FIELDS:
        payload[field] = _finite_cell(frame, pivot.pivot_index, field)
    return payload


def enrich_pivots(
    pivots: list[ConfirmedPivot],
    frame: pd.DataFrame,
    *,
    timeframe: str,
) -> list[dict[str, Any]]:
    return [enrich_pivot(p, frame, timeframe=timeframe) for p in pivots]


@dataclass(frozen=True)
class DevelopingSwingCandidate:
    candidate_index: int
    candidate_timestamp: str
    price: float
    pivot_type: PivotType
    missing_confirmation_candles: int
    earliest_confirmation_time: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_developing_swing_candidates(
    candles: pd.DataFrame,
    *,
    pivot_left: int,
    pivot_right: int,
    candle_interval_minutes: int,
    pivot_type: PivotType,
) -> list[DevelopingSwingCandidate]:
    """Local extremes that pass the left-window test but lack full right confirmation."""
    if candles.empty or pivot_left < 0 or pivot_right < 0:
        return []
    required = ("timestamp", "high", "low")
    missing = [c for c in required if c not in candles.columns]
    if missing:
        raise ValueError(f"candles missing columns for developing swings: {missing}")

    highs = pd.to_numeric(candles["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(candles["low"], errors="coerce").to_numpy(dtype=float)
    timestamps = list(candles["timestamp"])
    n = len(candles)
    step = pd.Timedelta(minutes=int(candle_interval_minutes))
    out: list[DevelopingSwingCandidate] = []

    for i in range(pivot_left, n):
        confirm_i = i + pivot_right
        if confirm_i < n:
            # Already confirmable inside the frame — not developing.
            continue
        if pivot_type == "high":
            value = highs[i]
            left_vals = highs[i - pivot_left : i]
            if (
                not np.isfinite(value)
                or left_vals.size != pivot_left
                or not np.all(np.isfinite(left_vals))
                or not bool(np.all(value > left_vals))
            ):
                continue
            # Partial right bars that already exist must still leave the high intact.
            right_vals = highs[i + 1 : n]
            if right_vals.size and not bool(np.all(value > right_vals)):
                continue
            price = float(value)
        else:
            value = lows[i]
            left_vals = lows[i - pivot_left : i]
            if (
                not np.isfinite(value)
                or left_vals.size != pivot_left
                or not np.all(np.isfinite(left_vals))
                or not bool(np.all(value < left_vals))
            ):
                continue
            right_vals = lows[i + 1 : n]
            if right_vals.size and not bool(np.all(value < right_vals)):
                continue
            price = float(value)

        missing = int(confirm_i - (n - 1))
        earliest = pd.Timestamp(timestamps[i]) + (pivot_right * step)
        if earliest.tzinfo is None:
            earliest = earliest.tz_localize("UTC")
        else:
            earliest = earliest.tz_convert("UTC")
        out.append(
            DevelopingSwingCandidate(
                candidate_index=i,
                candidate_timestamp=_ts_iso(timestamps[i]),
                price=price,
                pivot_type=pivot_type,
                missing_confirmation_candles=missing,
                earliest_confirmation_time=earliest.isoformat(),
            )
        )
    return out
