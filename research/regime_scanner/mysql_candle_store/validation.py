"""Candle frame validation helpers (UTC, OHLC integrity, gaps, alignment)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from research.regime_scanner.mysql_candle_store.candle_timeframes import (
    count_gaps,
    is_aligned_open,
    is_importable_timeframe,
    normalize_timeframe,
)
from research.regime_scanner.timeframes import ensure_utc_timestamp

REQUIRED_OHLCV = ("date", "open", "high", "low", "close", "volume")


@dataclass
class ValidationReport:
    rows_read: int = 0
    rows_valid: int = 0
    duplicate_timestamps: int = 0
    gap_count: int = 0
    gap_samples: list[dict[str, Any]] = field(default_factory=list)
    ohlc_violations: int = 0
    ohlc_violation_samples: list[dict[str, Any]] = field(default_factory=list)
    negative_volume_count: int = 0
    null_count: int = 0
    misaligned_opens: int = 0
    misaligned_samples: list[str] = field(default_factory=list)
    sorted: bool = True
    start: str | None = None
    end: str | None = None
    last_candle_closed: bool | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            not self.errors
            and self.ohlc_violations == 0
            and self.negative_volume_count == 0
            and self.misaligned_opens == 0
        )


def normalize_ohlcv_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=list(REQUIRED_OHLCV))
    out = df.copy()
    if "date" not in out.columns and "timestamp" in out.columns:
        out = out.rename(columns={"timestamp": "date"})
    missing = [c for c in REQUIRED_OHLCV if c not in out.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    out = out.loc[:, list(REQUIRED_OHLCV)].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.reset_index(drop=True)


def validate_ohlcv_frame(
    df: pd.DataFrame,
    *,
    timeframe: str,
    max_samples: int = 20,
    now: object | None = None,
) -> tuple[pd.DataFrame, ValidationReport]:
    """Validate and return cleaned frame + report.

    Only **closed** candles are retained when ``now`` is provided (default: UTC now).
    """
    from research.regime_scanner.mysql_candle_store.candle_timeframes import candle_close_time

    key = normalize_timeframe(timeframe)
    if not is_importable_timeframe(key):
        raise ValueError(f"unsupported timeframe for validation: {timeframe!r}")

    report = ValidationReport()
    frame = normalize_ohlcv_frame(df)
    report.rows_read = int(len(frame))
    if frame.empty:
        report.errors.append("empty candle frame")
        return frame, report

    if frame["date"].dt.tz is None:
        report.errors.append("timestamps are not timezone-aware UTC")
        return frame, report

    nulls = int(frame.isna().sum().sum())
    report.null_count = nulls
    if nulls:
        report.errors.append(f"null values present: {nulls}")

    dupes = int(frame["date"].duplicated().sum())
    report.duplicate_timestamps = dupes
    if dupes:
        report.errors.append(f"duplicate timestamps: {dupes}")

    report.sorted = bool(frame["date"].is_monotonic_increasing)
    if not report.sorted:
        frame = frame.sort_values("date").reset_index(drop=True)
        if int(frame["date"].duplicated().sum()):
            report.errors.append("duplicates remain after sort")
        report.sorted = True

    # Drop incomplete last candle(s) if still open.
    now_ts = ensure_utc_timestamp(now) if now is not None else pd.Timestamp.now(tz="UTC")
    close_times = frame["date"].map(lambda t: candle_close_time(t, key))
    closed_mask = close_times <= now_ts
    if len(frame) and not bool(closed_mask.iloc[-1]):
        report.last_candle_closed = False
        frame = frame.loc[closed_mask].reset_index(drop=True)
        close_times = frame["date"].map(lambda t: candle_close_time(t, key))
    else:
        report.last_candle_closed = True if len(frame) else None

    if frame.empty:
        report.errors.append("no closed candles after filtering open last bar")
        return frame, report

    misaligned_mask = [not is_aligned_open(ts, key) for ts in frame["date"]]
    misaligned = frame.loc[pd.Series(misaligned_mask, index=frame.index)]
    report.misaligned_opens = int(len(misaligned))
    report.misaligned_samples = [
        ensure_utc_timestamp(ts).isoformat() for ts in misaligned["date"].head(max_samples)
    ]
    if report.misaligned_opens:
        report.errors.append(
            f"misaligned open timestamps for {key}: {report.misaligned_opens}"
        )

    gaps, gap_samples = count_gaps(frame["date"], key)
    report.gap_count = gaps
    report.gap_samples = gap_samples

    neg_vol = frame["volume"] < 0
    report.negative_volume_count = int(neg_vol.sum())
    if report.negative_volume_count:
        report.errors.append(f"negative volume rows: {report.negative_volume_count}")

    ohlc_bad = (
        (frame["high"] < frame["open"])
        | (frame["high"] < frame["close"])
        | (frame["low"] > frame["open"])
        | (frame["low"] > frame["close"])
        | (frame["high"] < frame["low"])
    )
    report.ohlc_violations = int(ohlc_bad.sum())
    for _, row in frame.loc[ohlc_bad].head(max_samples).iterrows():
        report.ohlc_violation_samples.append(
            {
                "date": ensure_utc_timestamp(row["date"]).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    if report.ohlc_violations:
        report.errors.append(f"OHLC integrity violations: {report.ohlc_violations}")

    report.start = ensure_utc_timestamp(frame["date"].iloc[0]).isoformat()
    report.end = ensure_utc_timestamp(frame["date"].iloc[-1]).isoformat()
    report.rows_valid = int(len(frame)) if report.ok else 0
    return frame, report
