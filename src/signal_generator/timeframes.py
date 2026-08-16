"""Deterministic UTC higher-timeframe aggregation from closed 1m candles.

Shared history/live core. No stochastic / wave / signal logic here.

Validated wave-fade strategy TFs: ``STRATEGY_TIMEFRAMES`` = 15m / 30m / 1h / 4h.
``5m`` remains supported as a generic bucket size but is not a strategy signal TF.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence

TF_MINUTES: dict[str, int] = {
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
}

# Frozen fractal wave-fade signal / trend context timeframes (excl. 1m source).
STRATEGY_TIMEFRAMES: tuple[str, ...] = ("15m", "30m", "1h", "4h")


@dataclass(frozen=True, slots=True)
class OhlcvBar:
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float

    @property
    def available_at(self) -> datetime:
        """Earliest UTC instant this closed HTF bar may be used for signals."""
        return ensure_utc(self.close_time)


class BucketStatus(str, Enum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"
    NOT_CLOSED = "NOT_CLOSED"


@dataclass(frozen=True, slots=True)
class BucketInspection:
    timeframe: str
    bucket_open: datetime
    bucket_close: datetime
    status: BucketStatus
    reason: str
    expected_1m: int
    present_1m: int
    missing_open_times: tuple[datetime, ...]
    bar: OhlcvBar | None = None

    @property
    def available_at(self) -> datetime | None:
        if self.status != BucketStatus.COMPLETE or self.bar is None:
            return None
        return self.bar.available_at


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def bucket_start(ts: datetime, timeframe: str) -> datetime:
    """Return the UTC open_time of the higher-TF bucket containing ``ts``."""
    if timeframe not in TF_MINUTES:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    ts = ensure_utc(ts).replace(second=0, microsecond=0)
    minutes = TF_MINUTES[timeframe]
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    total_minutes = int((ts - epoch).total_seconds() // 60)
    floored = total_minutes - (total_minutes % minutes)
    return epoch + timedelta(minutes=floored)


def bucket_close(bucket_open: datetime, timeframe: str) -> datetime:
    return ensure_utc(bucket_open) + timedelta(minutes=TF_MINUTES[timeframe])


def expected_1m_count(timeframe: str) -> int:
    return TF_MINUTES[timeframe]


def is_bucket_closed(*, bucket_open: datetime, as_of: datetime, timeframe: str) -> bool:
    """A HTF candle is closed only when as_of >= bucket close_time (UTC)."""
    return ensure_utc(as_of) >= bucket_close(bucket_open, timeframe)


def is_htf_available_for_signals(
    *,
    bucket_open: datetime,
    timeframe: str,
    as_of: datetime,
) -> bool:
    """Signal engines may use the HTF bar only at/after ``available_at`` (== close_time)."""
    return is_bucket_closed(bucket_open=bucket_open, as_of=as_of, timeframe=timeframe)


def bar_from_mapping(row: Mapping[str, Any]) -> OhlcvBar:
    """Build ``OhlcvBar`` from Candle1m / CH dict / similar mapping."""
    ot = ensure_utc(row["open_time"])
    ct = row.get("close_time")
    if ct is None:
        ct = ot + timedelta(minutes=1)
    else:
        ct = ensure_utc(ct)
    return OhlcvBar(
        open_time=ot,
        close_time=ct,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
        turnover=float(row.get("turnover") or 0.0),
    )


def bars_from_mappings(rows: Sequence[Mapping[str, Any]]) -> list[OhlcvBar]:
    return [bar_from_mapping(r) for r in rows]


def _ohlcv_from_members(members: Sequence[OhlcvBar], *, start: datetime, close_time: datetime) -> OhlcvBar:
    return OhlcvBar(
        open_time=start,
        close_time=close_time,
        open=float(members[0].open),
        high=max(float(b.high) for b in members),
        low=min(float(b.low) for b in members),
        close=float(members[-1].close),
        volume=sum(float(b.volume) for b in members),
        turnover=sum(float(b.turnover) for b in members),
    )


def inspect_bucket(
    candles_1m: Sequence[OhlcvBar],
    *,
    timeframe: str,
    bucket_open: datetime,
    as_of: datetime | None = None,
) -> BucketInspection:
    """Inspect one UTC HTF bucket: COMPLETE only if closed in time and all 1m bars present."""
    if timeframe not in TF_MINUTES:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    start = ensure_utc(bucket_open)
    # Align to exact bucket boundary
    if bucket_start(start, timeframe) != start:
        start = bucket_start(start, timeframe)
    close_time = bucket_close(start, timeframe)
    expected = expected_1m_count(timeframe)
    expected_times = [start + timedelta(minutes=i) for i in range(expected)]

    by_ot = {ensure_utc(c.open_time): c for c in candles_1m}
    members = [by_ot[t] for t in expected_times if t in by_ot]
    missing = tuple(t for t in expected_times if t not in by_ot)

    if as_of is None:
        # Default: treat wall-clock as last available 1m close if any, else never closed.
        if members:
            as_of = max(ensure_utc(m.close_time) for m in members)
        else:
            as_of = start

    if not is_bucket_closed(bucket_open=start, as_of=as_of, timeframe=timeframe):
        return BucketInspection(
            timeframe=timeframe,
            bucket_open=start,
            bucket_close=close_time,
            status=BucketStatus.NOT_CLOSED,
            reason="bucket_not_yet_closed",
            expected_1m=expected,
            present_1m=len(members),
            missing_open_times=missing,
            bar=None,
        )

    if missing:
        return BucketInspection(
            timeframe=timeframe,
            bucket_open=start,
            bucket_close=close_time,
            status=BucketStatus.INCOMPLETE,
            reason="missing_1m_candles",
            expected_1m=expected,
            present_1m=len(members),
            missing_open_times=missing,
            bar=None,
        )

    members_sorted = [by_ot[t] for t in expected_times]
    bar = _ohlcv_from_members(members_sorted, start=start, close_time=close_time)
    return BucketInspection(
        timeframe=timeframe,
        bucket_open=start,
        bucket_close=close_time,
        status=BucketStatus.COMPLETE,
        reason="ok",
        expected_1m=expected,
        present_1m=expected,
        missing_open_times=(),
        bar=bar,
    )


def aggregate_bucket(
    candles_1m: Sequence[OhlcvBar],
    *,
    timeframe: str,
    bucket_open: datetime,
    as_of: datetime | None = None,
) -> OhlcvBar | None:
    """Live-friendly: aggregate one bucket. Returns None if not COMPLETE."""
    inspection = inspect_bucket(
        candles_1m,
        timeframe=timeframe,
        bucket_open=bucket_open,
        as_of=as_of,
    )
    return inspection.bar


def aggregate_1m_to_timeframe(
    candles_1m: Sequence[OhlcvBar],
    timeframe: str,
    *,
    as_of: datetime | None = None,
    require_complete: bool = True,
) -> list[OhlcvBar]:
    """Aggregate closed 1m bars into higher-TF closed bars (history/batch path).

    Incomplete buckets are skipped when ``require_complete`` is True (default).
    Never emits a bar for a bucket that is not yet closed at ``as_of``.
    """
    if timeframe not in TF_MINUTES:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}")
    if not candles_1m:
        return []

    sorted_bars = sorted((ensure_utc(c.open_time), c) for c in candles_1m)
    if as_of is None:
        as_of = ensure_utc(sorted_bars[-1][1].close_time)
    else:
        as_of = ensure_utc(as_of)

    bucket_opens = sorted({bucket_start(ot, timeframe) for ot, _ in sorted_bars})
    out: list[OhlcvBar] = []
    for start in bucket_opens:
        # Only pass 1m bars that can belong to this bucket (tiny window filter).
        close_t = bucket_close(start, timeframe)
        window = [
            bar
            for ot, bar in sorted_bars
            if start <= ot < close_t
        ]
        if require_complete:
            insp = inspect_bucket(window, timeframe=timeframe, bucket_open=start, as_of=as_of)
            if insp.bar is not None:
                out.append(insp.bar)
        else:
            # Legacy/partial path: still never emit before close; may build from present bars.
            if not is_bucket_closed(bucket_open=start, as_of=as_of, timeframe=timeframe):
                continue
            if not window:
                continue
            members = sorted(window, key=lambda b: ensure_utc(b.open_time))
            out.append(
                _ohlcv_from_members(members, start=start, close_time=close_t)
            )
    return out


def aggregate_strategy_timeframes(
    candles_1m: Sequence[OhlcvBar],
    *,
    as_of: datetime | None = None,
    timeframes: Sequence[str] = STRATEGY_TIMEFRAMES,
) -> dict[str, list[OhlcvBar]]:
    """Produce closed HTF bars for each strategy timeframe from the same 1m input."""
    return {
        tf: aggregate_1m_to_timeframe(candles_1m, tf, as_of=as_of, require_complete=True)
        for tf in timeframes
    }


def supported_timeframes() -> tuple[str, ...]:
    return tuple(TF_MINUTES.keys())


def strategy_timeframes() -> tuple[str, ...]:
    return STRATEGY_TIMEFRAMES


def iter_bucket_opens(
    start: datetime,
    end: datetime,
    timeframe: str,
) -> Iterable[datetime]:
    """Yield UTC bucket open times in [start, end)."""
    start = bucket_start(ensure_utc(start), timeframe)
    end = ensure_utc(end)
    step = timedelta(minutes=TF_MINUTES[timeframe])
    cur = start
    while cur < end:
        yield cur
        cur += step
