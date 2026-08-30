"""UTC / bucket helpers. Intervals are half-open [start, end)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def floor_second(dt: datetime) -> datetime:
    dt = ensure_utc(dt)
    return dt.replace(microsecond=0)


def bucket_close(sec: datetime) -> datetime:
    """A 1s bucket starting at `sec` closes at sec+1s."""
    return floor_second(sec) + timedelta(seconds=1)


def parse_utc(value: str) -> datetime:
    text = str(value).strip().replace("Z", "+00:00")
    return ensure_utc(datetime.fromisoformat(text))


def iso_z(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def align_floor(dt: datetime, step_s: int) -> datetime:
    dt = floor_second(dt)
    epoch = int(dt.timestamp())
    aligned = epoch - (epoch % int(step_s))
    return datetime.fromtimestamp(aligned, tz=timezone.utc)


def bps_move(start: float, end: float) -> float:
    if start is None or end is None or start <= 0:
        return float("nan")
    return (float(end) - float(start)) / float(start) * 10_000.0


def safe_finite(value: float, default: float = 0.0) -> float:
    import math

    if value is None:
        return default
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def percentile_rank(value: float, history: list[float]) -> float:
    """Past-only empirical CDF: fraction of history <= value. Empty → 0.5 neutral."""
    if not history:
        return 0.5
    n = sum(1 for x in history if x <= value)
    return n / len(history)


def invert_rank(rank: float) -> float:
    return 1.0 - float(rank)
