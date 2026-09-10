"""UTC period windows for previous-closed vs developing-as-of-focus."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orderbook_analyse.market_profile.anchor import PERIOD_SECONDS, as_utc, build_windows
from orderbook_analyse.market_profile.contracts import ProfileWindow

from ..timeparse import format_utc_z
from . import TIMEFRAMES


def period_s(tf: str) -> int:
    return int(PERIOD_SECONDS[tf])


def floor_period(ts: datetime, tf: str) -> datetime:
    ts = as_utc(ts)
    p = period_s(tf)
    u = int(ts.timestamp())
    return datetime.fromtimestamp(u - (u % p), tz=timezone.utc)


def previous_closed_bounds(focus_ts: datetime, tf: str) -> tuple[datetime, datetime]:
    cur = floor_period(focus_ts, tf)
    p = period_s(tf)
    start = datetime.fromtimestamp(int(cur.timestamp()) - p, tz=timezone.utc)
    return start, cur


def developing_bounds(focus_ts: datetime, tf: str) -> tuple[datetime, datetime]:
    cur = floor_period(focus_ts, tf)
    focus_ts = as_utc(focus_ts)
    return cur, focus_ts


def current_full_bounds(focus_ts: datetime, tf: str) -> tuple[datetime, datetime]:
    """Full UTC block containing focus — for FINAL_PROFILE_FOR_PARITY_ONLY only."""
    cur = floor_period(focus_ts, tf)
    p = period_s(tf)
    end = datetime.fromtimestamp(int(cur.timestamp()) + p, tz=timezone.utc)
    return cur, end


def make_window(tf: str, start: datetime, end: datetime, *, kind: str) -> ProfileWindow:
    start = as_utc(start)
    end = as_utc(end)
    t = int(start.timestamp())
    return ProfileWindow(
        window_id=f"{tf}_{t}_{kind}",
        anchor_mode=tf,
        label=f"{start.strftime('%Y-%m-%d %H:%M')} {tf} {kind}",
        start=start,
        end=end,
    )


def planned_windows(focus_ts: datetime) -> dict[str, Any]:
    focus_ts = as_utc(focus_ts)
    out: dict[str, Any] = {}
    for tf in TIMEFRAMES:
        prev_s, prev_e = previous_closed_bounds(focus_ts, tf)
        dev_s, dev_e = developing_bounds(focus_ts, tf)
        fin_s, fin_e = current_full_bounds(focus_ts, tf)
        out[tf] = {
            "previous_closed": {
                "start": format_utc_z(prev_s),
                "end": format_utc_z(prev_e),
                "role": "PREVIOUS_CLOSED_PROFILE_AVAILABLE_AT_FOCUS",
            },
            "developing": {
                "start": format_utc_z(dev_s),
                "end_exclusive": format_utc_z(dev_e),
                "role": "DEVELOPING_PROFILE_AS_OF_FOCUS",
            },
            "final_for_parity_only": {
                "start": format_utc_z(fin_s),
                "end": format_utc_z(fin_e),
                "role": "FINAL_PROFILE_FOR_PARITY_ONLY",
                "use_at_focus": False,
            },
        }
    return out


def assert_utc_alignment(focus_ts: datetime) -> list[dict[str, Any]]:
    """Prove period floors match dashboard build_windows."""
    focus_ts = as_utc(focus_ts)
    rows = []
    for tf in TIMEFRAMES:
        lo = floor_period(focus_ts, tf)
        hi = datetime.fromtimestamp(int(lo.timestamp()) + period_s(tf), tz=timezone.utc)
        built = build_windows(anchor_mode=tf, start=lo, end=hi)
        rows.append(
            {
                "timeframe": tf,
                "engine_start": format_utc_z(lo),
                "engine_end": format_utc_z(hi),
                "dashboard_n": len(built),
                "dashboard_start": format_utc_z(built[0].start) if built else None,
                "dashboard_end": format_utc_z(built[0].end) if built else None,
                "match": bool(built) and built[0].start == lo and built[0].end == hi,
            }
        )
    return rows
