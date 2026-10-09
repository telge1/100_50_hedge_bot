"""Coverage plan for the 143 unpublished-candle closes and preset warmup gates.

Does not read ClickHouse and does not publish a new candle file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from data.timeframes import SUPPORTED_TIMEFRAMES, expected_source_bars, timeframe_seconds

from ..identity import build_live_micro_lld_v2_config
from ..active_snapshot.rolling_calendar import rolling_history_from_close

BASE_SNAPSHOT_ID = "dba0ebaf41432e81177b8dc0c52dd252f9302506a72833b5f049290bd8509ebc"
BASE_PRICE_HASH = "093b2bd57f6525a7886c0bc3c76cd27c24ef4e51d640f155fe410b435e4acd28"
BASE_REVISION_HASH = "df049cde8eb41cf916847b9063f7f4ddd1a894d574ba713e9568c2b4c8bf4f25"
BASE_RANGE_START = datetime(2026, 5, 23, 5, 25, tzinfo=timezone.utc)
BASE_RANGE_END_EXCLUSIVE = datetime(2026, 9, 30, 12, 1, tzinfo=timezone.utc)
READY_FIRST = datetime(2026, 9, 24, 0, 0, tzinfo=timezone.utc)
READY_LAST = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
MISSING_FIRST = datetime(2026, 9, 30, 12, 5, tzinfo=timezone.utc)
MISSING_LAST = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
TAIL_RANGE_START = datetime(2026, 9, 30, 12, 1, tzinfo=timezone.utc)
TAIL_RANGE_END_EXCLUSIVE = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(timezone.utc)


def preset_warmup_bars(pool_detail: int) -> int:
    cfg = build_live_micro_lld_v2_config(int(pool_detail))
    return int(cfg.volume_lookback) + int(cfg.swing_len) + 64


def decision_1m_opens(decision_close: datetime, timeframe: str) -> tuple[datetime, datetime]:
    step = timeframe_seconds(timeframe)
    dc = _utc(decision_close)
    if int(dc.timestamp()) % step != 0:
        raise ValueError("decision close is not on the timeframe grid")
    first = dc - timedelta(seconds=step)
    last = dc - timedelta(seconds=60)
    return first, last


def warmup_source_start(decision_close: datetime, timeframe: str, pool_detail: int) -> datetime:
    """Earliest 1m open required by the rolling-4-month window plus original warmup."""
    dc = _utc(decision_close)
    history_from = rolling_history_from_close(dc, 4)
    tf_sec = timeframe_seconds(timeframe)
    load_start = int(history_from.timestamp()) - preset_warmup_bars(pool_detail) * tf_sec
    need = expected_source_bars("1m", timeframe)
    return datetime.fromtimestamp(load_start - need * 60, tz=timezone.utc)


def base_file_status(
    decision_close: datetime,
    timeframe: str,
    pool_detail: int,
    *,
    range_start: datetime = BASE_RANGE_START,
    range_end_exclusive: datetime = BASE_RANGE_END_EXCLUSIVE,
) -> str:
    if timeframe not in SUPPORTED_TIMEFRAMES or timeframe == "1m":
        return "INVALID_REQUEST"
    if pool_detail < 1 or pool_detail > 10:
        return "INVALID_REQUEST"
    try:
        bar_first, bar_last = decision_1m_opens(decision_close, timeframe)
        src = warmup_source_start(decision_close, timeframe, pool_detail)
    except ValueError:
        return "INVALID_REQUEST"
    start = _utc(range_start)
    end = _utc(range_end_exclusive)
    if src < start or bar_first < start or bar_last >= end:
        return "INSUFFICIENT_CANDLE_COVERAGE"
    return "READY"


def five_minute_closes(first: datetime, last: datetime) -> list[datetime]:
    step = timedelta(minutes=5)
    out = []
    cur = _utc(first)
    end = _utc(last)
    while cur <= end:
        out.append(cur)
        cur += step
    return out


def tail_extension_plan() -> dict:
    missing = five_minute_closes(MISSING_FIRST, MISSING_LAST)
    tail_minutes = int((TAIL_RANGE_END_EXCLUSIVE - TAIL_RANGE_START).total_seconds() // 60)
    return {
        "base_snapshot_id": BASE_SNAPSHOT_ID,
        "base_file_unchanged": True,
        "base_range_start": BASE_RANGE_START.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_range_end_exclusive": BASE_RANGE_END_EXCLUSIVE.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "missing_close_first": MISSING_FIRST.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "missing_close_last": MISSING_LAST.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "missing_close_count": len(missing),
        "additional_1m_first_open": TAIL_RANGE_START.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "additional_1m_last_open": (TAIL_RANGE_END_EXCLUSIVE - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "additional_1m_count": tail_minutes,
        "tail_range_end_exclusive": TAIL_RANGE_END_EXCLUSIVE.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contiguous_with_base": TAIL_RANGE_START == BASE_RANGE_END_EXCLUSIVE,
        "publish_beside_base": True,
        "replace_base": False,
        "known_at_history_stays_on_base": True,
        "coverage_rule": (
            "A close is READY only when the rolling warmup source start lies inside the unchanged "
            "base file and every 1m open of the decision bar lies in the contiguous base-plus-tail union. "
            "A gap, a hash mismatch, or a second snapshot id for the same close fails closed."
        ),
    }


def support_matrix(decision_close: datetime = READY_LAST) -> list[dict]:
    rows = []
    for timeframe in ("5m", "15m", "30m", "1h", "4h"):
        for pool_detail in range(1, 11):
            rows.append(
                {
                    "timeframe": timeframe,
                    "pool_detail": pool_detail,
                    "warmup_bars": preset_warmup_bars(pool_detail),
                    "source_start": warmup_source_start(decision_close, timeframe, pool_detail).strftime(
                        "%Y-%m-%dT%H:%M:%SZ"
                    ),
                    "base_file": base_file_status(decision_close, timeframe, pool_detail),
                }
            )
    return rows


def independent_parity_status(publication_rows: int) -> str:
    """PASS is refused unless exactly one independent publication exists to compare."""
    if publication_rows == 1:
        return "COMPARABLE"
    return "NOT_PROVEN"
