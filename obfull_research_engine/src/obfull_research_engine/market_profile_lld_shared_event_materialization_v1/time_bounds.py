"""UTC profile window bounds. Uses dashboard period seconds; no VA math."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from orderbook_analyse.market_profile.anchor import PERIOD_SECONDS, as_utc


def floor_period(ts: datetime, timeframe: str) -> datetime:
    ts = as_utc(ts)
    period = int(PERIOD_SECONDS[timeframe])
    unix = int(ts.timestamp())
    return datetime.fromtimestamp(unix - (unix % period), tz=timezone.utc)


def natural_period_containing(ts: datetime, timeframe: str) -> tuple[datetime, datetime]:
    start = floor_period(ts, timeframe)
    end = start + timedelta(seconds=int(PERIOD_SECONDS[timeframe]))
    return start, end


def resolve_profile_window(
    *,
    request_as_of: datetime,
    timeframe: str,
    requested_state: str,
) -> dict[str, Any]:
    """Map a requested CLOSED/DEVELOPING state onto UTC [start, end) bounds.

    CLOSED at a period boundary uses the period that just ended.
    DEVELOPING uses [floor(as_of), as_of) and requires as_of inside the period.
    """
    as_of = as_utc(request_as_of)
    tf = str(timeframe)
    if tf not in PERIOD_SECONDS:
        raise ValueError(f"unsupported timeframe: {tf}")
    state = str(requested_state).strip().upper()
    period = timedelta(seconds=int(PERIOD_SECONDS[tf]))
    floored = floor_period(as_of, tf)
    current_end = floored + period

    if state == "DEVELOPING":
        natural_start, natural_end = floored, current_end
        effective_end = as_of
        if effective_end <= natural_start or effective_end >= natural_end:
            state = "INVALID"
        profile_state = state
    elif state == "CLOSED":
        if as_of == floored:
            natural_start, natural_end = floored - period, floored
        else:
            natural_start, natural_end = floored - period, floored
        effective_end = natural_end
        if as_of < natural_end:
            profile_state = "INVALID"
        else:
            profile_state = "CLOSED"
    else:
        natural_start = natural_end = effective_end = floored
        profile_state = "INVALID"

    return {
        "timeframe": tf,
        "request_as_of": as_of,
        "profile_state": profile_state,
        "natural_profile_start": natural_start,
        "natural_profile_end": natural_end,
        "effective_profile_end": effective_end,
        "uses_data_until_exclusive": effective_end,
    }


def last_included_1m_close(effective_end: datetime) -> datetime:
    """Close time of the last 1m candle with open_time < effective_end."""
    end = as_utc(effective_end)
    last_open = datetime.fromtimestamp(int(end.timestamp()) - 60, tz=timezone.utc)
    return last_open + timedelta(seconds=60)
