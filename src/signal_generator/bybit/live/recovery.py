"""REST gap recovery using the existing BybitHistoryClient (no second pipeline)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from signal_generator.bybit.history import (
    BybitHistoryClient,
    SOURCE as HISTORY_SOURCE,
    chunk_batches,
    ensure_utc,
)
from signal_generator.bybit.missing_ranges import detect_missing_ranges
from signal_generator.db.candles import CandleRepository
from signal_generator.db.client import ClickHouseClient

logger = logging.getLogger(__name__)

LIVE_RECOVERY_SOURCE = HISTORY_SOURCE  # same canonical normalizer/source family


@dataclass(slots=True)
class SymbolRecoveryResult:
    symbol: str
    last_stored_open: datetime | None
    recovery_start: datetime | None
    recovery_end: datetime | None
    gap_minutes: int
    fetched: int
    inserted: int
    validated_final: int
    ok: bool
    error: str | None = None
    internal_ranges_repaired: int = 0
    internal_candles_inserted: int = 0
    cold_start: bool = False


def last_fully_closed_open_time(*, as_of: datetime | None = None) -> datetime:
    """Open time of the latest 1m candle that is fully closed at ``as_of``."""
    as_of = ensure_utc(as_of or datetime.now(timezone.utc))
    floored = as_of.replace(second=0, microsecond=0)
    return floored - timedelta(minutes=1)


def compute_recovery_window(
    last_stored_open: datetime | None,
    *,
    as_of: datetime | None = None,
    overlap_minutes: int = 1,
) -> tuple[datetime, datetime] | None:
    """Return half-open [start, end) REST window, or None if nothing to recover."""
    as_of = ensure_utc(as_of or datetime.now(timezone.utc))
    end = last_fully_closed_open_time(as_of=as_of) + timedelta(minutes=1)
    if last_stored_open is None:
        start = end - timedelta(minutes=1)
        if start >= end:
            return None
        return start, end

    last_stored_open = ensure_utc(last_stored_open)
    next_needed = last_stored_open + timedelta(minutes=1)
    if next_needed >= end:
        return None

    start = last_stored_open - timedelta(minutes=max(0, overlap_minutes - 1))
    if overlap_minutes <= 0:
        start = next_needed
    return start, end


def recover_symbol(
    *,
    symbol: str,
    repo: CandleRepository,
    history: BybitHistoryClient,
    as_of: datetime | None = None,
    overlap_minutes: int = 1,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> SymbolRecoveryResult:
    """Recover trailing missing closed 1m candles for one symbol via REST."""
    as_of = ensure_utc(as_of or datetime.now(timezone.utc))
    last_stored = repo.get_last_closed_open_time(symbol)
    cold_start = last_stored is None
    window = compute_recovery_window(
        last_stored, as_of=as_of, overlap_minutes=overlap_minutes
    )
    if window is None:
        return SymbolRecoveryResult(
            symbol=symbol,
            last_stored_open=last_stored,
            recovery_start=None,
            recovery_end=None,
            gap_minutes=0,
            fetched=0,
            inserted=0,
            validated_final=0,
            ok=True,
            cold_start=cold_start,
        )

    start, end = window
    gap_minutes = int(
        (
            end - (last_stored + timedelta(minutes=1) if last_stored else start)
        ).total_seconds()
        // 60
    )
    if last_stored is None:
        gap_minutes = int((end - start).total_seconds() // 60)

    try:
        candles = history.fetch_closed_1m(symbol, start, end, as_of=as_of)
        inserted = 0
        if not dry_run and candles:
            for batch in chunk_batches(candles, batch_size):
                inserted += repo.insert_candles(batch)
        validated = repo.count_final(symbol, start, end)
        logger.info(
            "RECOVERY %s last_stored=%s window=[%s, %s) fetched=%s inserted=%s final=%s cold=%s",
            symbol,
            last_stored,
            start.isoformat(),
            end.isoformat(),
            len(candles),
            inserted,
            validated,
            cold_start,
        )
        return SymbolRecoveryResult(
            symbol=symbol,
            last_stored_open=last_stored,
            recovery_start=start,
            recovery_end=end,
            gap_minutes=max(gap_minutes, 0),
            fetched=len(candles),
            inserted=inserted,
            validated_final=validated,
            ok=True,
            cold_start=cold_start,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("RECOVERY failed for %s: %s", symbol, exc)
        return SymbolRecoveryResult(
            symbol=symbol,
            last_stored_open=last_stored,
            recovery_start=start,
            recovery_end=end,
            gap_minutes=max(gap_minutes, 0),
            fetched=0,
            inserted=0,
            validated_final=0,
            ok=False,
            error=str(exc),
            cold_start=cold_start,
        )


def repair_internal_missing_ranges(
    *,
    symbol: str,
    ch: ClickHouseClient,
    repo: CandleRepository,
    history: BybitHistoryClient,
    as_of: datetime | None = None,
    batch_size: int = 1000,
    lookback_days: int = 30,
) -> tuple[int, int]:
    """REST-fill coalesced missing ranges inside recent CH coverage."""
    as_of = ensure_utc(as_of or datetime.now(timezone.utc))
    end = last_fully_closed_open_time(as_of=as_of) + timedelta(minutes=1)
    last = repo.get_last_closed_open_time(symbol)
    if last is None:
        return 0, 0

    floor = end - timedelta(days=max(1, lookback_days))
    report = detect_missing_ranges(
        ch,
        symbol=symbol,
        effective_start=floor,
        requested_end=end,
    )
    ranges_repaired = 0
    inserted = 0
    for mr in report.ranges:
        candles = history.fetch_closed_1m(symbol, mr.start, mr.end, as_of=as_of)
        if candles:
            for batch in chunk_batches(candles, batch_size):
                inserted += repo.insert_candles(batch)
        ranges_repaired += 1
        logger.info(
            "INTERNAL_REPAIR %s kind=%s [%s, %s) fetched=%s",
            symbol,
            mr.kind,
            mr.start.isoformat(),
            mr.end.isoformat(),
            len(candles),
        )
    return ranges_repaired, inserted


def repair_recent_continuity(
    *,
    symbol: str,
    ch: ClickHouseClient,
    repo: CandleRepository,
    history: BybitHistoryClient,
    as_of: datetime | None = None,
    lookback_minutes: int = 120,
    batch_size: int = 1000,
) -> tuple[int, int]:
    """FILL_MISSING in a short recent window (reconnect mid-gap safety).

    Trailing recovery alone misses holes when MAX(open_time) jumped past a
    lost minute (e.g. last=15:44, WS later wrote 15:46, 15:45 absent).
    """
    as_of = ensure_utc(as_of or datetime.now(timezone.utc))
    end = last_fully_closed_open_time(as_of=as_of) + timedelta(minutes=1)
    last = repo.get_last_closed_open_time(symbol)
    if last is None:
        return 0, 0
    lookback = max(2, int(lookback_minutes))
    floor = end - timedelta(minutes=lookback)
    # Never scan before we have coverage; clamp to a sane floor
    if last < floor:
        # Still scan from floor — holes between floor and end matter
        pass
    report = detect_missing_ranges(
        ch,
        symbol=symbol,
        effective_start=floor,
        requested_end=end,
    )
    ranges_repaired = 0
    inserted = 0
    for mr in report.ranges:
        candles = history.fetch_closed_1m(symbol, mr.start, mr.end, as_of=as_of)
        if candles:
            for batch in chunk_batches(candles, batch_size):
                inserted += repo.insert_candles(batch)
        ranges_repaired += 1
        logger.info(
            "RECENT_CONTINUITY %s kind=%s [%s, %s) fetched=%s",
            symbol,
            mr.kind,
            mr.start.isoformat(),
            mr.end.isoformat(),
            len(candles),
        )
    return ranges_repaired, inserted


def recover_symbol_full(
    *,
    symbol: str,
    ch: ClickHouseClient,
    repo: CandleRepository,
    history: BybitHistoryClient,
    as_of: datetime | None = None,
    overlap_minutes: int = 1,
    batch_size: int = 1000,
    repair_internal: bool = True,
    internal_lookback_days: int = 30,
    recent_continuity_minutes: int = 120,
) -> SymbolRecoveryResult:
    """Trailing recovery + optional internal missing-range repair."""
    result = recover_symbol(
        symbol=symbol,
        repo=repo,
        history=history,
        as_of=as_of,
        overlap_minutes=overlap_minutes,
        batch_size=batch_size,
    )
    if not result.ok:
        return result
    if repair_internal and not result.cold_start:
        try:
            n_ranges, n_ins = repair_internal_missing_ranges(
                symbol=symbol,
                ch=ch,
                repo=repo,
                history=history,
                as_of=as_of,
                batch_size=batch_size,
                lookback_days=internal_lookback_days,
            )
            result.internal_ranges_repaired = n_ranges
            result.internal_candles_inserted = n_ins
            result.inserted += n_ins
        except Exception as exc:  # noqa: BLE001
            logger.exception("internal repair failed for %s: %s", symbol, exc)
            result.ok = False
            result.error = f"internal_repair: {exc}"
            return result
        # Short recent continuity pass (covers tip-jump mid-gaps like lost 15:45)
        try:
            n2, i2 = repair_recent_continuity(
                symbol=symbol,
                ch=ch,
                repo=repo,
                history=history,
                as_of=as_of,
                lookback_minutes=recent_continuity_minutes,
                batch_size=batch_size,
            )
            result.internal_ranges_repaired += n2
            result.internal_candles_inserted += i2
            result.inserted += i2
        except Exception as exc:  # noqa: BLE001
            logger.exception("recent continuity failed for %s: %s", symbol, exc)
            result.ok = False
            result.error = f"recent_continuity: {exc}"
    return result


def recover_symbols(
    symbols: Sequence[str],
    *,
    repo: CandleRepository,
    history: BybitHistoryClient,
    as_of: datetime | None = None,
    overlap_minutes: int = 1,
    batch_size: int = 1000,
) -> list[SymbolRecoveryResult]:
    return [
        recover_symbol(
            symbol=s,
            repo=repo,
            history=history,
            as_of=as_of,
            overlap_minutes=overlap_minutes,
            batch_size=batch_size,
        )
        for s in symbols
    ]


def recover_symbols_full(
    symbols: Sequence[str],
    *,
    ch: ClickHouseClient,
    repo: CandleRepository,
    history: BybitHistoryClient,
    as_of: datetime | None = None,
    overlap_minutes: int = 1,
    batch_size: int = 1000,
    repair_internal: bool = True,
    internal_lookback_days: int = 30,
    recent_continuity_minutes: int = 120,
) -> list[SymbolRecoveryResult]:
    return [
        recover_symbol_full(
            symbol=s,
            ch=ch,
            repo=repo,
            history=history,
            as_of=as_of,
            overlap_minutes=overlap_minutes,
            batch_size=batch_size,
            repair_internal=repair_internal,
            internal_lookback_days=internal_lookback_days,
            recent_continuity_minutes=recent_continuity_minutes,
        )
        for s in symbols
    ]
