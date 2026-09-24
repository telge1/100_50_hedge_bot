"""UTC minute-aligned signal window. Does not silently shift the requested start."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stoch_universe_51.update_plan import last_closed_end_exclusive

from .config import DEFAULT_SIGNAL_START, MAX_WINDOW_DAYS


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("UTC_REQUIRED")
    return ts.astimezone(timezone.utc)


def parse_utc_minute(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("INVALID_DATETIME")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("INVALID_DATETIME") from exc
    dt = _ensure_utc(dt)
    if dt.second != 0 or dt.microsecond != 0:
        raise ValueError("MINUTE_ALIGNMENT_REQUIRED")
    return dt


def iso_z(ts: datetime) -> str:
    return _ensure_utc(ts).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def suggested_end_exclusive(now: datetime | None = None) -> datetime:
    return last_closed_end_exclusive(now)


def suggested_start() -> datetime:
    return DEFAULT_SIGNAL_START


def validate_window(
    start: datetime,
    end: datetime,
    *,
    now: datetime | None = None,
) -> None:
    start = _ensure_utc(start)
    end = _ensure_utc(end)
    if start.second or start.microsecond or end.second or end.microsecond:
        raise ValueError("MINUTE_ALIGNMENT_REQUIRED")
    if start >= end:
        raise ValueError("START_NOT_BEFORE_END")
    max_end = suggested_end_exclusive(now)
    if end > max_end:
        raise ValueError("END_IN_OPEN_OR_FUTURE_MINUTE")
    span = end - start
    if span > timedelta(days=MAX_WINDOW_DAYS):
        raise ValueError("WINDOW_TOO_LARGE")
