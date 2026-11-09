"""L1 V3J — derive backtest window from manual drawings (research only)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from line_geometry import BAR_SEC_5M, LineDef, fmt_utc, utc_ts

DRAWINGS_AUTO_WINDOW = "DRAWINGS_AUTO_WINDOW"
STATUS_NO_ELIGIBLE = "NO_ELIGIBLE_SUPPORT_LINES"
STATUS_LINES_EXHAUSTED = "SUPPORT_LINES_EXHAUSTED"
STATUS_DATA_END = "DATA_END_REACHED"
STATUS_OK = "OK"


@dataclass(frozen=True)
class AutoWindow:
    window_start_ts: int
    window_end_ts: int  # exclusive: last bar to include + 5m
    data_end_ts: int
    support_line_ids: tuple[str, ...]
    status: str
    per_line: tuple[dict[str, Any], ...]


def is_candidate_support_trend(line: LineDef, *, upper_main_id: str) -> bool:
    if line.drawing_type != "trend":
        return False
    if line.drawing_id == upper_main_id:
        return False
    return line.t_lo is not None and line.t_hi is not None


def line_research_end_ts(line: LineDef, data_end_bar_open: int) -> int:
    """Last 5m bar open on which the line may still produce touches."""
    if line.t_hi is None:
        return data_end_bar_open
    if line.extend_right:
        return data_end_bar_open
    return int(line.t_hi)


def compute_auto_window(
    lines: list[LineDef],
    *,
    upper_main_id: str,
    data_end_bar_open: int,
) -> AutoWindow:
    support = [ln for ln in lines if is_candidate_support_trend(ln, upper_main_id=upper_main_id)]
    if not support:
        t0 = utc_ts(datetime(2026, 8, 25, tzinfo=timezone.utc))
        return AutoWindow(t0, t0, data_end_bar_open, (), STATUS_NO_ELIGIBLE, ())

    per: list[dict[str, Any]] = []
    starts: list[int] = []
    ends: list[int] = []
    for ln in support:
        t_lo = int(ln.t_lo)  # type: ignore[arg-type]
        t_end = line_research_end_ts(ln, data_end_bar_open)
        starts.append(t_lo)
        ends.append(t_end)
        per.append(
            {
                "drawing_id": ln.drawing_id,
                "extend_right": ln.extend_right,
                "anchor_t1_utc": fmt_utc(int(ln.t1)) if ln.t1 else "",
                "anchor_t2_utc": fmt_utc(int(ln.t2)) if ln.t2 else "",
                "research_start_utc": fmt_utc(t_lo),
                "research_end_utc": fmt_utc(t_end),
            }
        )

    w_start = min(starts)
    w_end = max(ends)
    w_end_exclusive = w_end + BAR_SEC_5M

    last_support_end = max(ends)
    if last_support_end < data_end_bar_open:
        status = STATUS_LINES_EXHAUSTED
    elif w_end_exclusive >= data_end_bar_open + BAR_SEC_5M:
        status = STATUS_DATA_END
    else:
        status = STATUS_OK

    return AutoWindow(
        window_start_ts=w_start,
        window_end_ts=min(w_end_exclusive, data_end_bar_open + BAR_SEC_5M),
        data_end_ts=data_end_bar_open,
        support_line_ids=tuple(sorted(ln.drawing_id for ln in support)),
        status=status,
        per_line=tuple(per),
    )


def fetch_last_available_bar_open(symbol: str, timeframe: str, *, bootstrap) -> int:
    """Last complete bar open timestamp available from candle SSOT."""
    bootstrap()
    from research_charts.service import candle_objects

    probe_start = utc_ts(datetime(2020, 1, 1, tzinfo=timezone.utc))
    probe_end = utc_ts(datetime(2030, 1, 1, tzinfo=timezone.utc))
    rows = candle_objects(symbol, timeframe, start=probe_start, end=probe_end, limit=50000, allow_stale=True)
    if not rows:
        return probe_start
    last_t = None
    for c in rows:
        ts = getattr(c, "timestamp", None) or getattr(c, "time", None)
        if ts is None:
            continue
        last_t = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
    return int(last_t) if last_t is not None else probe_start
