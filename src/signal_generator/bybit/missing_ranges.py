"""Detect coalesced missing 1m ranges in ClickHouse candle history.

Semantik: half-open ``[start, end)`` UTC minute buckets.
Pre-listing is handled by callers via ``effective_start``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Sequence

from signal_generator.bybit.history import ensure_utc
from signal_generator.db.client import ClickHouseClient

RangeKind = Literal["LEADING", "INTERNAL", "TRAILING", "FULL"]

DEFAULT_MAX_MISSING_RANGES = 10_000
DEFAULT_MAX_REPAIR_SPAN_MINUTES = 400_000  # ~278 days


@dataclass(frozen=True, slots=True)
class MissingRange:
    start: datetime  # inclusive
    end: datetime  # exclusive
    kind: RangeKind

    @property
    def missing_minutes(self) -> int:
        return max(0, int((self.end - self.start).total_seconds() // 60))

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": ensure_utc(self.start).isoformat(),
            "end": ensure_utc(self.end).isoformat(),
            "kind": self.kind,
            "missing_minutes": self.missing_minutes,
        }


@dataclass(slots=True)
class MissingRangeReport:
    symbol: str
    effective_start: datetime
    requested_end: datetime
    existing_count: int
    ranges: list[MissingRange]
    truncated: bool = False
    truncate_reason: str = ""

    @property
    def missing_candles(self) -> int:
        return sum(r.missing_minutes for r in self.ranges)

    @property
    def missing_range_count(self) -> int:
        return len(self.ranges)


def _minute(ts: datetime) -> datetime:
    ts = ensure_utc(ts)
    return ts.replace(second=0, microsecond=0)


def missing_ranges_from_open_times(
    open_times: Sequence[datetime],
    *,
    effective_start: datetime,
    requested_end: datetime,
) -> list[MissingRange]:
    """Coalesce missing 1m buckets in ``[effective_start, requested_end)``.

    ``open_times`` must be unique closed-candle opens (any order).
    """
    start = _minute(effective_start)
    end = _minute(requested_end)
    if end <= start:
        return []

    present = sorted(
        {
            _minute(t)
            for t in open_times
            if start <= _minute(t) < end
        }
    )
    if not present:
        return [MissingRange(start=start, end=end, kind="FULL")]

    out: list[MissingRange] = []
    # Leading
    if present[0] > start:
        out.append(MissingRange(start=start, end=present[0], kind="LEADING"))

    # Internal
    for prev, nxt in zip(present, present[1:]):
        expected_next = prev + timedelta(minutes=1)
        if nxt > expected_next:
            out.append(MissingRange(start=expected_next, end=nxt, kind="INTERNAL"))

    # Trailing
    last = present[-1]
    trailing_start = last + timedelta(minutes=1)
    if trailing_start < end:
        out.append(MissingRange(start=trailing_start, end=end, kind="TRAILING"))

    return out


def apply_safety_limits(
    ranges: list[MissingRange],
    *,
    max_ranges: int = DEFAULT_MAX_MISSING_RANGES,
    max_span_minutes: int = DEFAULT_MAX_REPAIR_SPAN_MINUTES,
) -> tuple[list[MissingRange], bool, str]:
    """Return (possibly truncated) ranges chronologically; never silent empty."""
    if not ranges:
        return [], False, ""
    total_span = sum(r.missing_minutes for r in ranges)
    if len(ranges) > max_ranges:
        kept = ranges[:max_ranges]
        return (
            kept,
            True,
            f"missing_ranges={len(ranges)} exceeds max_ranges={max_ranges}; "
            f"processing first {max_ranges} chronologically",
        )
    if total_span > max_span_minutes:
        # Keep chronological ranges until span budget exhausted
        kept: list[MissingRange] = []
        used = 0
        for r in ranges:
            if used + r.missing_minutes > max_span_minutes and kept:
                break
            kept.append(r)
            used += r.missing_minutes
        return (
            kept,
            True,
            f"missing_span={total_span}m exceeds max_span_minutes={max_span_minutes}; "
            f"processing {len(kept)}/{len(ranges)} ranges ({used}m)",
        )
    return ranges, False, ""


def fetch_open_times_final(
    client: ClickHouseClient,
    *,
    symbol: str,
    effective_start: datetime,
    requested_end: datetime,
    exchange: str = "bybit",
    interval: str = "1m",
) -> list[datetime]:
    """Load distinct closed 1m open_times via FINAL in [effective_start, requested_end)."""
    start = ensure_utc(effective_start)
    end = ensure_utc(requested_end)
    result = client.query(
        f"""
        SELECT open_time
        FROM {client.database}.candles_1m FINAL
        WHERE exchange = {{exchange:String}}
          AND symbol = {{symbol:String}}
          AND interval = {{interval:String}}
          AND is_closed = 1
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        ORDER BY open_time ASC
        """,
        parameters={
            "exchange": exchange,
            "symbol": symbol,
            "interval": interval,
            "start": start,
            "end": end,
        },
    )
    out: list[datetime] = []
    for (ot,) in result.result_rows:
        out.append(ensure_utc(ot) if getattr(ot, "tzinfo", None) is None else ensure_utc(ot))
    return out


def detect_missing_ranges(
    client: ClickHouseClient | None,
    *,
    symbol: str,
    effective_start: datetime,
    requested_end: datetime,
    exchange: str = "bybit",
    interval: str = "1m",
    open_times: Sequence[datetime] | None = None,
    max_ranges: int = DEFAULT_MAX_MISSING_RANGES,
    max_span_minutes: int = DEFAULT_MAX_REPAIR_SPAN_MINUTES,
) -> MissingRangeReport:
    """Detect coalesced missing ranges (from CH FINAL or provided open_times)."""
    start = ensure_utc(effective_start)
    end = ensure_utc(requested_end)
    if open_times is None:
        if client is None:
            raise ValueError("client required when open_times not provided")
        open_times = fetch_open_times_final(
            client,
            symbol=symbol,
            effective_start=start,
            requested_end=end,
            exchange=exchange,
            interval=interval,
        )
    ranges = missing_ranges_from_open_times(
        open_times, effective_start=start, requested_end=end
    )
    limited, truncated, reason = apply_safety_limits(
        ranges, max_ranges=max_ranges, max_span_minutes=max_span_minutes
    )
    return MissingRangeReport(
        symbol=symbol,
        effective_start=start,
        requested_end=end,
        existing_count=len(
            {_minute(t) for t in open_times if start <= _minute(t) < end}
        ),
        ranges=limited,
        truncated=truncated,
        truncate_reason=reason,
    )
