"""Live candle refresh helpers (no strategy logic)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


@dataclass
class SymbolPollResult:
    symbol: str
    status: str
    bar_close: str | None = None
    bars_processed: int = 0
    catchup_bars: int = 0
    duplicate_bars: int = 0
    missed_bars: int = 0
    out_of_order: int = 0
    new_1m_bars: int = 0
    geometry_candidates: int = 0
    ladder_blocked: int = 0
    allowed: int = 0
    open_shadow: int = 0
    closed_tp: int = 0
    closed_sl: int = 0
    closed_be: int = 0
    first_seen_in_ch: str | None = None
    processing_started_at: str | None = None
    processing_finished_at: str | None = None
    errors: list[str] = field(default_factory=list)
    signal_rows: list[dict] = field(default_factory=list)


def utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def new_bars_after(bars: list, last_processed: datetime | None, time_key: str = "close_time") -> list:
    if not bars:
        return []
    if last_processed is None:
        return list(bars)
    last_u = utc(last_processed)
    return [b for b in bars if utc(b[time_key]) > last_u]


def missed_15m_closes(last_processed: datetime, latest_close: datetime, new_bars: list) -> int:
    last_u, latest_u = utc(last_processed), utc(latest_close)
    if latest_u <= last_u:
        return 0
    have = {utc(b["close_time"]) for b in new_bars}
    missed = 0
    t = last_u + timedelta(minutes=15)
    while t <= latest_u:
        if t not in have:
            missed += 1
        t += timedelta(minutes=15)
    return missed


def scan_new_bar_metrics(new_bars: list, last_processed: datetime | None) -> tuple[int, int]:
    duplicate = 0
    out_of_order = 0
    prev = utc(last_processed) if last_processed is not None else None
    for bar in new_bars:
        ct = utc(bar["close_time"])
        if prev is not None and ct <= prev:
            if ct == prev:
                duplicate += 1
            else:
                out_of_order += 1
            continue
        prev = ct
    return duplicate, out_of_order
