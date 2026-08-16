"""ClickHouse-side quality aggregations for large candle histories.

Avoids pulling multi-month FINAL series into Python for every symbol.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from signal_generator.bybit.history import ensure_utc, expected_candle_count
from signal_generator.bybit.quality import (
    GapRecord,
    SymbolQualityReport,
    classify_coverage_window,
    classify_final_status,
)
from signal_generator.db.client import ClickHouseClient


def audit_symbol_sql(
    client: ClickHouseClient,
    *,
    symbol: str,
    requested_start: datetime,
    requested_end: datetime,
    effective_start: datetime | None = None,
    checkpoint_status: str | None = None,
    exchange: str = "bybit",
    interval: str = "1m",
    gap_sample_limit: int = 50,
) -> SymbolQualityReport:
    """Compute quality metrics using FINAL aggregations in ClickHouse."""
    requested_start = ensure_utc(requested_start)
    requested_end = ensure_utc(requested_end)
    db = client.database
    table = f"{db}.candles_1m"

    agg = client.query(
        f"""
        SELECT
            count() AS unique_final,
            min(open_time) AS min_ot,
            max(open_time) AS max_ot,
            countIf(
                high < greatest(open, close)
                OR low > least(open, close)
                OR high < low
                OR volume < 0
                OR turnover < 0
            ) AS ohlc_errors,
            countIf(close_time != open_time + toIntervalMinute(1)) AS close_time_errors
        FROM {table} FINAL
        WHERE exchange = {{exchange:String}}
          AND symbol = {{symbol:String}}
          AND interval = {{interval:String}}
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
          AND is_closed = 1
        """,
        parameters={
            "exchange": exchange,
            "symbol": symbol,
            "interval": interval,
            "start": requested_start,
            "end": requested_end,
        },
    )
    row = agg.result_rows[0]
    unique_final = int(row[0])
    min_ot = row[1]
    max_ot = row[2]
    ohlc_error_count = int(row[3])
    close_time_errors = int(row[4])
    if min_ot is not None and getattr(min_ot, "tzinfo", None) is None:
        min_ot = ensure_utc(min_ot)
    if max_ot is not None and getattr(max_ot, "tzinfo", None) is None:
        max_ot = ensure_utc(max_ot)

    physical = client.query(
        f"""
        SELECT count()
        FROM {table}
        WHERE exchange = {{exchange:String}}
          AND symbol = {{symbol:String}}
          AND interval = {{interval:String}}
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        """,
        parameters={
            "exchange": exchange,
            "symbol": symbol,
            "interval": interval,
            "start": requested_start,
            "end": requested_end,
        },
    )
    physical_rows = int(physical.result_rows[0][0])

    eff = ensure_utc(effective_start) if effective_start is not None else min_ot
    gaps: list[GapRecord] = []
    if unique_final >= 2:
        gap_q = client.query(
            f"""
            SELECT
                prev_ot,
                open_time AS next_ot,
                dateDiff('second', prev_ot, open_time) AS gap_seconds
            FROM (
                SELECT
                    open_time,
                    lagInFrame(open_time, 1, open_time) OVER (
                        ORDER BY open_time
                        ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                    ) AS prev_ot
                FROM {table} FINAL
                WHERE exchange = {{exchange:String}}
                  AND symbol = {{symbol:String}}
                  AND interval = {{interval:String}}
                  AND open_time >= {{start:DateTime64(3, 'UTC')}}
                  AND open_time < {{end:DateTime64(3, 'UTC')}}
                  AND is_closed = 1
            )
            WHERE open_time > prev_ot
              AND dateDiff('second', prev_ot, open_time) != 60
            ORDER BY prev_ot
            LIMIT {{lim:UInt32}}
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "start": requested_start,
                "end": requested_end,
                "lim": gap_sample_limit,
            },
        )
        for prev_ot, next_ot, gap_seconds in gap_q.result_rows:
            prev_ot = ensure_utc(prev_ot) if getattr(prev_ot, "tzinfo", None) is None else ensure_utc(prev_ot)
            next_ot = ensure_utc(next_ot) if getattr(next_ot, "tzinfo", None) is None else ensure_utc(next_ot)
            gap_seconds = int(gap_seconds)
            missing = max(gap_seconds // 60 - 1, 0)
            gaps.append(
                GapRecord(
                    symbol=symbol,
                    previous_open_time=prev_ot,
                    next_open_time=next_ot,
                    gap_seconds=gap_seconds,
                    missing_candle_count=missing,
                    gap_class="INTERNAL_DATA_GAP",
                )
            )

        # Exact gap_count / missing totals (not limited sample)
        gap_stats = client.query(
            f"""
            SELECT
                count() AS gap_count,
                max(gap_seconds) AS largest_gap_seconds,
                sum(intDiv(gap_seconds, 60) - 1) AS missing_candles
            FROM (
                SELECT dateDiff('second', prev_ot, open_time) AS gap_seconds
                FROM (
                    SELECT
                        open_time,
                        lagInFrame(open_time, 1, open_time) OVER (
                            ORDER BY open_time
                            ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                        ) AS prev_ot
                    FROM {table} FINAL
                    WHERE exchange = {{exchange:String}}
                      AND symbol = {{symbol:String}}
                      AND interval = {{interval:String}}
                      AND open_time >= {{start:DateTime64(3, 'UTC')}}
                      AND open_time < {{end:DateTime64(3, 'UTC')}}
                      AND is_closed = 1
                )
                WHERE open_time > prev_ot
                  AND dateDiff('second', prev_ot, open_time) != 60
            )
            """,
            parameters={
                "exchange": exchange,
                "symbol": symbol,
                "interval": interval,
                "start": requested_start,
                "end": requested_end,
            },
        )
        gs = gap_stats.result_rows[0]
        gap_count = int(gs[0] or 0)
        largest_gap_seconds = int(gs[1] or 0)
        missing_from_internal = int(gs[2] or 0)
    else:
        gap_count = 0
        largest_gap_seconds = 0
        missing_from_internal = 0

    pre_listing, trailing = classify_coverage_window(
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=eff,
        max_open_time=max_ot,
    )
    for g in trailing:
        g.symbol = symbol
        gaps.append(g)
        gap_count += 1
        largest_gap_seconds = max(largest_gap_seconds, g.gap_seconds)
        missing_from_internal += g.missing_candle_count

    coverage_start = eff or requested_start
    expected = expected_candle_count(coverage_start, requested_end) if unique_final or eff else 0

    # Build dummy ohlc error list length for reporting (details omitted at scale)
    final_status = classify_final_status(
        unique_final=unique_final,
        gap_count=gap_count,
        ohlc_error_count=ohlc_error_count,
        close_time_errors=close_time_errors,
        checkpoint_status=checkpoint_status,
    )
    if checkpoint_status == "COMPLETE" and unique_final == 0:
        final_status = "NO_HISTORY"

    return SymbolQualityReport(
        symbol=symbol,
        expected=expected,
        unique_final=unique_final,
        physical_rows=physical_rows,
        min_open_time=min_ot,
        max_open_time=max_ot,
        duplicate_logical_keys=0,
        gap_count=gap_count,
        largest_gap_seconds=largest_gap_seconds,
        gaps=gaps[:gap_sample_limit],
        ohlc_errors=[],
        close_time_errors=close_time_errors,
        crosscheck_pass=None,
        effective_start=eff,
        requested_start=requested_start,
        requested_end=requested_end,
        pre_listing_minutes=pre_listing,
        missing_candle_count=missing_from_internal,
        final_status=final_status,
        ohlc_error_count=ohlc_error_count,
    )


def summarize_totals_sql(
    client: ClickHouseClient,
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    start = ensure_utc(start)
    end = ensure_utc(end)
    r = client.query(
        f"""
        SELECT
            count() AS physical,
            uniqExact((exchange, symbol, interval, open_time)) AS approx_unique
        FROM {client.database}.candles_1m
        WHERE exchange = 'bybit'
          AND interval = '1m'
          AND symbol IN {{symbols:Array(String)}}
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        """,
        parameters={"symbols": symbols, "start": start, "end": end},
    )
    physical, approx_unique = r.result_rows[0]
    # True FINAL unique total
    r2 = client.query(
        f"""
        SELECT count()
        FROM {client.database}.candles_1m FINAL
        WHERE exchange = 'bybit'
          AND interval = '1m'
          AND symbol IN {{symbols:Array(String)}}
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
          AND is_closed = 1
        """,
        parameters={"symbols": symbols, "start": start, "end": end},
    )
    return {
        "physical": int(physical),
        "approx_unique_key": int(approx_unique),
        "unique_final": int(r2.result_rows[0][0]),
    }
