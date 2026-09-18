"""UTC date-window and symbol-list helpers for the existing public-trade backfill CLI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from signal_generator.bybit.live.candle_universe import (
    PUBLIC_TRADE_BLOCKED_SYMBOLS,
    load_candle_universe,
    normalize_symbols,
)
from signal_generator.bybit.public_trades.urls import utc_day_bounds

DEFAULT_7D_START = date(2026, 8, 10)
DEFAULT_7D_END_EXCLUSIVE = date(2026, 8, 17)
DEFAULT_7D_EXPECTED_FILES = 357
SMOKE_SYMBOL = "BTCUSDT"
SMOKE_DAY = date(2026, 7, 19)


class BackfillCliError(ValueError):
    """Fail-closed CLI / window error."""


def inclusive_utc_days(start: date, end_exclusive: date) -> list[date]:
    """Return [start, end_exclusive) as UTC calendar days."""
    if end_exclusive <= start:
        raise BackfillCliError(
            f"invalid window: start {start.isoformat()} is not before "
            f"end-exclusive {end_exclusive.isoformat()}"
        )
    days: list[date] = []
    cur = start
    while cur < end_exclusive:
        days.append(cur)
        cur += timedelta(days=1)
    if not days:
        raise BackfillCliError("invalid window: zero days")
    return days


def parse_utc_date(value: str) -> date:
    text = str(value).strip()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise BackfillCliError(f"invalid UTC date: {value!r}") from exc


def parse_symbol_csv(raw: str | None) -> list[str]:
    if raw is None or not str(raw).strip():
        return []
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    return normalize_symbols(parts)


def load_universe_symbols(path: Path) -> list[str]:
    symbols = load_candle_universe(path)
    blocked = [s for s in symbols if s in PUBLIC_TRADE_BLOCKED_SYMBOLS]
    if blocked:
        raise BackfillCliError("XAUUSDT must not be added to public trades")
    return symbols


def event_window_covers_utc_day(
    min_ts: datetime | None,
    max_ts: datetime | None,
    day: date,
    *,
    first_trade_grace_s: int = 300,
    last_trade_grace_s: int = 300,
) -> bool:
    """True when first/last events sit at the UTC day edges.

    Empty minutes inside the day are not treated as gaps. A live-only morning
    hole (first trade after 00:05) is incomplete.
    """
    if min_ts is None or max_ts is None:
        return False
    start, end = utc_day_bounds(day)
    if min_ts.tzinfo is None:
        min_ts = min_ts.replace(tzinfo=timezone.utc)
    else:
        min_ts = min_ts.astimezone(timezone.utc)
    if max_ts.tzinfo is None:
        max_ts = max_ts.replace(tzinfo=timezone.utc)
    else:
        max_ts = max_ts.astimezone(timezone.utc)
    if min_ts < start or max_ts >= end:
        return False
    if min_ts >= start + timedelta(seconds=first_trade_grace_s):
        return False
    if max_ts < end - timedelta(seconds=last_trade_grace_s):
        return False
    return True


def classify_symbol_day(
    *,
    logical_unique: int,
    min_ts: datetime | None,
    max_ts: datetime | None,
    sources: Sequence[str],
    day: date,
    manifest_status: str | None = None,
) -> str:
    if manifest_status == "AUDITED":
        return "ALREADY_AUDITED"
    if logical_unique <= 0:
        return "MISSING"
    src = {str(s).lower() for s in sources}
    complete = event_window_covers_utc_day(min_ts, max_ts, day) and "archive" in src
    if complete:
        return "ALREADY_AUDITED"
    return "PARTIAL_IN_CLICKHOUSE"


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    start: date
    end_exclusive: date
    days: tuple[date, ...]
    symbols: tuple[str, ...]
    universe_file: Path
    expected_files: int
    legacy_7d: bool
    smoke: bool
    strict_sources: bool


def resolve_backfill_plan(
    *,
    start_date: str | None,
    end_date_exclusive: str | None,
    symbols_csv: str | None,
    universe_file: Path,
    smoke: bool,
) -> BackfillPlan:
    universe = load_universe_symbols(universe_file)
    requested = parse_symbol_csv(symbols_csv)

    if smoke:
        if start_date and parse_utc_date(start_date) != SMOKE_DAY:
            raise BackfillCliError("--smoke requires start-date 2026-07-19")
        if end_date_exclusive and parse_utc_date(end_date_exclusive) != date(2026, 7, 20):
            raise BackfillCliError("--smoke requires --end-date-exclusive 2026-07-20")
        if requested and requested != [SMOKE_SYMBOL]:
            raise BackfillCliError("--smoke allows only BTCUSDT")
        start = SMOKE_DAY
        end_excl = date(2026, 7, 20)
        symbols = (SMOKE_SYMBOL,)
        days = tuple(inclusive_utc_days(start, end_excl))
        return BackfillPlan(
            start=start,
            end_exclusive=end_excl,
            days=days,
            symbols=symbols,
            universe_file=universe_file,
            expected_files=1,
            legacy_7d=False,
            smoke=True,
            strict_sources=False,
        )

    if (start_date is None) ^ (end_date_exclusive is None):
        raise BackfillCliError("both --start-date and --end-date-exclusive are required")

    if start_date is None:
        start = DEFAULT_7D_START
        end_excl = DEFAULT_7D_END_EXCLUSIVE
        legacy = True
    else:
        start = parse_utc_date(start_date)
        end_excl = parse_utc_date(end_date_exclusive or "")
        legacy = False

    days = tuple(inclusive_utc_days(start, end_excl))
    if requested:
        unknown = [s for s in requested if s not in set(universe)]
        if unknown:
            raise BackfillCliError(
                "symbols not in universe: " + ",".join(unknown)
            )
        symbols = tuple(requested)
    else:
        symbols = tuple(universe)

    expected = len(symbols) * len(days)
    if legacy and not requested:
        if expected != DEFAULT_7D_EXPECTED_FILES:
            raise BackfillCliError(
                f"legacy 7d expected {DEFAULT_7D_EXPECTED_FILES} files, got {expected}"
            )
    return BackfillPlan(
        start=start,
        end_exclusive=end_excl,
        days=days,
        symbols=symbols,
        universe_file=universe_file,
        expected_files=expected,
        legacy_7d=legacy and not requested,
        smoke=False,
        strict_sources=legacy and not requested,
    )
