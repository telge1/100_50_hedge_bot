"""Bybit historical public-trade URL and UTC day helpers."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

PUBLIC_BYBIT_TRADING_BASE = "https://public.bybit.com/trading"
FILENAME_RE = r"^(?P<symbol>[A-Za-z0-9]+)(?P<date>\d{4}-\d{2}-\d{2})\.csv\.gz$"


def daily_filename(symbol: str, day: date) -> str:
    return f"{symbol.upper()}{day.isoformat()}.csv.gz"


def daily_url(symbol: str, day: date) -> str:
    sym = symbol.upper()
    return f"{PUBLIC_BYBIT_TRADING_BASE}/{sym}/{daily_filename(sym, day)}"


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def parse_utc_day(value: str) -> date:
    return date.fromisoformat(str(value).strip()[:10])
