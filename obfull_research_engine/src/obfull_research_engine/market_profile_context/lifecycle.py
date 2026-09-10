"""Post-focus breakdown / rebound — never writes back into at-focus state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..timeparse import format_utc_z


def _floor5(u: int) -> int:
    return u - (u % 300)


def five_m_ohlc_from_trades(
    trades: list[Any],
    *,
    start_unix: int,
    end_unix: int,
) -> list[dict[str, Any]]:
    """Closed 5m candles with candle_end <= end_unix, from public trades."""
    buckets: dict[int, list[float]] = {}
    for t in trades:
        ts = t.trade_ts
        if hasattr(ts, "timestamp"):
            u = int(ts.timestamp())
        else:
            u = int(ts)
        if u < start_unix or u >= end_unix:
            continue
        b = _floor5(u)
        buckets.setdefault(b, []).append(float(t.price))
    rows = []
    for start in sorted(buckets):
        end = start + 300
        if end > end_unix:
            continue  # only fully closed 5m
        px = buckets[start]
        rows.append(
            {
                "candle_start": format_utc_z(datetime.fromtimestamp(start, tz=timezone.utc)),
                "candle_end": format_utc_z(datetime.fromtimestamp(end, tz=timezone.utc)),
                "candle_start_unix": start,
                "candle_end_unix": end,
                "open": px[0],
                "high": max(px),
                "low": min(px),
                "close": px[-1],
                "close_below_edge": None,
                "close_above_edge": None,
            }
        )
    return rows


def lifecycle_after_focus(
    *,
    candles: list[dict[str, Any]],
    edge_level: float | None,
    side: str,
    at_focus_state: str,
) -> dict[str, Any]:
    if edge_level is None or not candles:
        return {
            "one_5m_close": None,
            "two_5m_closes": None,
            "three_5m_closes": None,
            "n_closes_beyond": 0,
            "final_lifecycle": "UNRESOLVED",
            "reclaim": False,
            "at_focus_state_unchanged": at_focus_state,
        }
    beyond = []
    for c in candles:
        if side == "lower":
            c["close_below_edge"] = float(c["close"]) < edge_level
            beyond.append(bool(c["close_below_edge"]))
        else:
            c["close_above_edge"] = float(c["close"]) > edge_level
            beyond.append(bool(c["close_above_edge"]))

    n = 0
    streak = 0
    max_streak = 0
    for b in beyond:
        if b:
            n += 1
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    one = "ONE_5M_CLOSE_BELOW" if side == "lower" and max_streak >= 1 else (
        "ONE_5M_CLOSE_ABOVE" if side == "upper" and max_streak >= 1 else None
    )
    two = "TWO_5M_CLOSES_BELOW" if side == "lower" and max_streak >= 2 else (
        "TWO_5M_CLOSES_ABOVE" if side == "upper" and max_streak >= 2 else None
    )
    three = "THREE_5M_CLOSES_BELOW" if side == "lower" and max_streak >= 3 else (
        "THREE_5M_CLOSES_ABOVE" if side == "upper" and max_streak >= 3 else None
    )

    last_beyond = beyond[-1] if beyond else False
    first_beyond = any(beyond[:1])
    later_back = any((not b) for b in beyond[1:]) if len(beyond) > 1 else False
    reclaim = first_beyond and later_back and not last_beyond

    if max_streak >= 3 and last_beyond:
        final = "ACCEPTED_BELOW" if side == "lower" else "ACCEPTED_ABOVE"
    elif reclaim:
        final = "FAILED_BREAKDOWN" if side == "lower" else "FAILED_BREAKOUT"
    elif max_streak >= 1 and last_beyond:
        final = "UNRESOLVED"
    elif reclaim:
        final = "LOWER_EDGE_RECLAIM" if side == "lower" else "UPPER_EDGE_RECLAIM"
    else:
        final = "UNRESOLVED"

    if reclaim and final == "FAILED_BREAKDOWN":
        reclaim_label = "LOWER_EDGE_REJECTION"
    elif reclaim:
        reclaim_label = "LOWER_EDGE_RECLAIM" if side == "lower" else "UPPER_EDGE_RECLAIM"
    else:
        reclaim_label = None

    return {
        "one_5m_close": one,
        "two_5m_closes": two,
        "three_5m_closes": three,
        "n_closes_beyond": n,
        "max_consecutive_closes_beyond": max_streak,
        "reclaim": reclaim,
        "reclaim_label": reclaim_label,
        "final_lifecycle": final,
        "strong_confirm_requires_ge_2": True,
        "one_close_is_not_strong_confirm": max_streak < 2,
        "at_focus_state_unchanged": at_focus_state,
    }
