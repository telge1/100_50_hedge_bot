"""Timeframe duration helpers for EDC candidate windows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def timeframe_duration(timeframe: str) -> timedelta:
    tf = str(timeframe).strip().lower()
    if tf.endswith("m"):
        return timedelta(minutes=int(tf[:-1]))
    if tf.endswith("h"):
        return timedelta(hours=int(tf[:-1]))
    raise ValueError(f"unsupported timeframe: {timeframe}")


def bar_close(bar_open: datetime, timeframe: str) -> datetime:
    return _utc(bar_open) + timeframe_duration(timeframe)
