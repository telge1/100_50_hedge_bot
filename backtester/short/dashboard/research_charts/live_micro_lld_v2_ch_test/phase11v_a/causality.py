"""Close-boundary checks. Pools from one timeframe are not treated as another."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.timeframes import timeframe_seconds

from ..phase11u_c.coverage import decision_1m_opens


def close_is_on_grid(decision_close: datetime, timeframe: str) -> bool:
    step = timeframe_seconds(timeframe)
    dc = decision_close.astimezone(timezone.utc)
    return int(dc.timestamp()) % step == 0


def decision_bar_is_complete(decision_close: datetime, timeframe: str) -> bool:
    first, last = decision_1m_opens(decision_close, timeframe)
    step = timeframe_seconds(timeframe)
    expected = step // 60
    minutes = int((last - first).total_seconds() // 60) + 1
    return minutes == expected and last == decision_close.astimezone(timezone.utc) - timedelta(seconds=60)


def future_candle(close_unix: int, decision_close: datetime) -> bool:
    return int(close_unix) > int(decision_close.astimezone(timezone.utc).timestamp())


def reject_reappearance(previous_fully_traversed: bool, next_remaining_visible: bool) -> str:
    if previous_fully_traversed and next_remaining_visible:
        return "FAIL"
    return "PASS"


def bounds_unchanged(original_bottom: float, original_top: float, segment_bottom: float, segment_top: float) -> bool:
    return segment_bottom == original_bottom and segment_top == original_top
