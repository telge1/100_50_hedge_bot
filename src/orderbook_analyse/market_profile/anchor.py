"""Window anchoring — where a profile starts and stops.

This is the part that decides what a profile actually means. A profile
computed over a window the market does not care about produces value-area
edges at prices that never carried liquidity, so the anchor has to match the
regime being analysed rather than a convenient calendar boundary.

Modes:

``5m`` / ``15m`` / ``30m`` / ``1h`` / ``4h``
    One profile per UTC-aligned clock block of that duration. Partial first
    and last blocks are clipped to the requested range (forming included).
``day``
    One profile per UTC calendar day.
``session``
    One profile per liquidity session per day (see :data:`SESSIONS`). This is
    the crypto stand-in for "use cash-session profiles if you trade the cash
    session".
``composite``
    A single merged profile over the whole requested range. Use this for a
    balance period that spans several days.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import DEFAULT_SESSIONS, SESSIONS
from .contracts import ProfileWindow

PERIOD_SECONDS: dict[str, int] = {
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
}


def as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _session_bounds(day: datetime, session: str) -> tuple[datetime, datetime]:
    try:
        sh, sm, eh, em = SESSIONS[session]
    except KeyError as exc:
        raise ValueError(f"unknown session: {session!r}") from exc
    start = day.replace(hour=sh, minute=sm, second=0, microsecond=0)
    if eh >= 24:
        end = day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
            days=1, hours=eh - 24, minutes=em
        )
    else:
        end = day.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start, end


def _clip(
    start: datetime, end: datetime, lo: datetime, hi: datetime
) -> tuple[datetime, datetime] | None:
    s = max(start, lo)
    e = min(end, hi)
    if e <= s:
        return None
    return s, e


def _period_windows(
    *,
    mode: str,
    lo: datetime,
    hi: datetime,
) -> list[ProfileWindow]:
    """UTC-aligned fixed-duration windows; edges clipped to ``[lo, hi)``."""
    period_s = PERIOD_SECONDS[mode]
    lo_epoch = int(lo.timestamp())
    hi_epoch = int(hi.timestamp())
    # Floor to the UTC period that contains ``lo`` so a mid-block start still
    # yields a (clipped) forming/partial window rather than skipping it.
    t = (lo_epoch // period_s) * period_s
    out: list[ProfileWindow] = []
    while t < hi_epoch:
        s = datetime.fromtimestamp(t, tz=timezone.utc)
        e = datetime.fromtimestamp(t + period_s, tz=timezone.utc)
        clipped = _clip(s, e, lo, hi)
        if clipped is not None:
            cs, ce = clipped
            out.append(
                ProfileWindow(
                    window_id=f"{mode}_{t}",
                    anchor_mode=mode,
                    label=f"{s.strftime('%Y-%m-%d %H:%M')} {mode}",
                    start=cs,
                    end=ce,
                )
            )
        t += period_s
    return out


def build_windows(
    *,
    anchor_mode: str,
    start: datetime,
    end: datetime,
    sessions: tuple[str, ...] | list[str] | None = None,
) -> list[ProfileWindow]:
    """Generate the profile windows for a request.

    Windows are clipped to ``[start, end)``; a window that collapses to zero
    duration after clipping is dropped, so a partial first or last day never
    produces a phantom profile.
    """
    lo, hi = as_utc(start), as_utc(end)
    if hi <= lo:
        raise ValueError("end must be after start")

    mode = str(anchor_mode).strip().lower()

    if mode in PERIOD_SECONDS:
        return _period_windows(mode=mode, lo=lo, hi=hi)

    if mode == "composite":
        label = f"{lo.date().isoformat()}..{(hi - timedelta(microseconds=1)).date().isoformat()}"
        return [
            ProfileWindow(
                window_id="composite",
                anchor_mode="composite",
                label=f"COMPOSITE {label}",
                start=lo,
                end=hi,
            )
        ]

    if mode == "day":
        out: list[ProfileWindow] = []
        day = lo.replace(hour=0, minute=0, second=0, microsecond=0)
        while day < hi:
            nxt = day + timedelta(days=1)
            clipped = _clip(day, nxt, lo, hi)
            if clipped is not None:
                s, e = clipped
                out.append(
                    ProfileWindow(
                        window_id=f"day_{day.date().isoformat()}",
                        anchor_mode="day",
                        label=day.date().isoformat(),
                        start=s,
                        end=e,
                    )
                )
            day = nxt
        return out

    if mode == "session":
        wanted = tuple(sessions or DEFAULT_SESSIONS)
        for name in wanted:
            if name not in SESSIONS:
                raise ValueError(f"unknown session: {name!r}")
        out = []
        # `late` ends after midnight, so start one day early to catch a
        # session that began before the requested window opened.
        day = lo.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        last = hi.replace(hour=0, minute=0, second=0, microsecond=0)
        while day <= last:
            for name in wanted:
                s0, e0 = _session_bounds(day, name)
                clipped = _clip(s0, e0, lo, hi)
                if clipped is None:
                    continue
                s, e = clipped
                out.append(
                    ProfileWindow(
                        window_id=f"{day.date().isoformat()}_{name}",
                        anchor_mode="session",
                        label=f"{day.date().isoformat()} {name.upper()}",
                        start=s,
                        end=e,
                    )
                )
            day += timedelta(days=1)
        out.sort(key=lambda w: (w.start, w.window_id))
        return out

    raise ValueError(f"unknown anchor_mode: {anchor_mode!r}")
