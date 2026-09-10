"""Strict UTC timestamp parsing for OBFULL CLI (Z required)."""

from __future__ import annotations

from datetime import datetime, timezone


class CliUsageError(ValueError):
    """Invalid user input → exit 64."""


def parse_utc_z(value: str, *, field: str) -> datetime:
    raw = (value or "").strip()
    if not raw.endswith("Z"):
        raise CliUsageError(
            f"{field} must be an explicit UTC timestamp ending with 'Z' "
            f"(got {value!r}). Naive timestamps are rejected."
        )
    if "+" in raw[:-1] or raw.count("-") > 2 and "T" in raw and raw.rfind("-") > raw.find("T"):
        # disallow offsets other than Z
        body = raw[:-1]
        if "+" in body or body.endswith("-00:00"):
            raise CliUsageError(f"{field} must use Z, not a numeric offset: {value!r}")
    try:
        # Accept 2026-09-06T10:00:00Z and with fractional seconds
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CliUsageError(f"{field} is not a valid ISO-8601 UTC timestamp: {value!r}") from exc
    if dt.tzinfo is None:
        raise CliUsageError(f"{field} rejected naive timestamp: {value!r}")
    return dt.astimezone(timezone.utc)


def validate_interval(start: datetime, end: datetime) -> None:
    if start >= end:
        raise CliUsageError(f"start must be < end (got start={start.isoformat()} end={end.isoformat()})")


def format_utc_z(dt: datetime) -> str:
    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_duration(start: datetime, end: datetime) -> str:
    secs = int((end - start).total_seconds())
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    if h and m:
        return f"{h}h {m:02d}m"
    if h:
        return f"{h}h 00m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"
