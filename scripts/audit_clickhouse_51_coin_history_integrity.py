#!/usr/bin/env python3
"""AUDIT_CLICKHOUSE_51_COIN_HISTORY_INTEGRITY — audit-only, no mutations.

Validates ClickHouse candles_1m coverage/integrity for the tradeable-51 universe
after the 8-month backfill. Uses FINAL for ReplacingMergeTree logical counts.
"""

from __future__ import annotations

import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from signal_generator.bybit.history import (  # noqa: E402
    BybitHistoryClient,
    expected_candle_count,
    ensure_utc,
    millis_to_utc,
)
from signal_generator.bybit.universe import (  # noqa: E402
    INSTRUMENTS_URL,
    filter_active_linear_usdt_perpetuals,
)
from signal_generator.bybit.history import HttpxTransport  # noqa: E402
from signal_generator.db.client import get_client  # noqa: E402
from signal_generator.db.candles import CandleRepository  # noqa: E402
from signal_generator.timeframes import (  # noqa: E402
    bucket_start,
)

UNIVERSE_PATH = ROOT / "config" / "universe_tradeable_51.json"
LOG_PATH = ROOT / "logs" / "backfill_tradeable_51_8m.log"
OUT = ROOT / "results" / "clickhouse_51_coin_history_integrity_audit"

REQUESTED_START = datetime(2025, 12, 11, tzinfo=timezone.utc)
REQUESTED_END = datetime(2026, 8, 11, tzinfo=timezone.utc)  # exclusive

SPOT_SYMBOLS = ("BTCUSDT", "DOGEUSDT", "APTUSDT", "LITUSDT")
FOCUS = ("LITUSDT", "ETHUSDT", "BTCUSDT", "XRPUSDT", "SOLUSDT", "DOGEUSDT", "APTUSDT", "RENDERUSDT")

# Gap thresholds for verdict
MINOR_GAP_MAX_MISSING = 60  # minutes
MINOR_GAP_MAX_LONGEST = 30  # minutes


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                cols.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _iso(ts: datetime | None) -> str | None:
    if ts is None:
        return None
    return ensure_utc(ts).isoformat().replace("+00:00", "Z")


def _ceil_minute(ts: datetime) -> datetime:
    ts = ensure_utc(ts)
    if ts.second == 0 and ts.microsecond == 0:
        return ts.replace(microsecond=0)
    return (ts.replace(second=0, microsecond=0) + timedelta(minutes=1))


def load_universe() -> list[str]:
    raw = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    symbols = [str(s).upper() for s in raw["symbols"]]
    return symbols


def parse_backfill_log(path: Path) -> dict[str, dict[str, Any]]:
    """Extract final quality lines: final=x/y gaps=n quality=STATUS."""
    out: dict[str, dict[str, Any]] = {}
    pat = re.compile(
        r"\[(\d+)/(\d+)\]\s+(\w+)\s+quality=(\w+)\s+final=(\d+)/(\d+)\s+gaps=(\d+)"
    )
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = pat.search(line)
        if not m:
            continue
        sym = m.group(3).upper()
        out[sym] = {
            "log_index": int(m.group(1)),
            "log_total": int(m.group(2)),
            "log_quality": m.group(4),
            "log_final_actual": int(m.group(5)),
            "log_final_expected": int(m.group(6)),
            "log_gaps": int(m.group(7)),
        }
    return out


def fetch_instrument_details(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Map symbol → instrument row (launchTime, contractType, status, settleCoin)."""
    transport = HttpxTransport()
    # Prefer per-symbol lookups for accuracy; fall back to full list if needed.
    details: dict[str, dict[str, Any]] = {}
    for sym in symbols:
        try:
            payload = transport.get_json(
                INSTRUMENTS_URL, {"category": "linear", "symbol": sym}
            )
            rows = (payload.get("result") or {}).get("list") or []
            if rows:
                details[sym] = rows[0]
        except Exception as exc:  # noqa: BLE001
            details[sym] = {"_error": str(exc)}
    return details


def first_available_bybit_open(
    hist: BybitHistoryClient,
    symbol: str,
    *,
    start: datetime,
    end: datetime,
    probe_hours: int = 48,
) -> datetime | None:
    """Find earliest Bybit 1m open_time in [start, end) via short probes."""
    start = ensure_utc(start)
    end = ensure_utc(end)
    cursor = start
    while cursor < end:
        probe_end = min(cursor + timedelta(hours=probe_hours), end)
        try:
            rows = hist.fetch_closed_1m(symbol, cursor, probe_end, max_pages=5)
        except Exception:  # noqa: BLE001
            return None
        if rows:
            return ensure_utc(rows[0].open_time)
        cursor = probe_end
    return None


def expected_start_for(
    symbol: str,
    detail: dict[str, Any] | None,
    *,
    requested_start: datetime,
    hist: BybitHistoryClient | None = None,
) -> tuple[datetime | None, str]:
    """Return (expected_start, start_source).

    Prefer max(requested_start, ceil(launchTime), first_available_bybit) so
    exchange-empty pre-trade minutes after launch are not counted as gaps.
    """
    if not detail or detail.get("_error"):
        base = requested_start
        src = "REQUESTED_START_NO_LAUNCH"
        launch_ceil = None
    else:
        from signal_generator.bybit.universe import UniverseSymbol, launch_time_utc

        launch = launch_time_utc(
            UniverseSymbol(
                symbol=symbol,
                rank=0,
                turnover24h="0",
                volume24h="0",
                launch_time=(
                    millis_to_utc(detail["launchTime"]).isoformat()
                    if detail.get("launchTime") not in (None, "", "0")
                    else None
                ),
                contract_type=str(detail.get("contractType") or ""),
                status=str(detail.get("status") or ""),
                settle_coin=str(detail.get("settleCoin") or ""),
            )
        )
        if launch is None:
            base = requested_start
            src = "REQUESTED_START_NO_LAUNCH"
            launch_ceil = None
        else:
            launch_ceil = _ceil_minute(launch)
            if launch_ceil <= requested_start:
                base = requested_start
                src = "REQUESTED_START"
            else:
                base = launch_ceil
                src = "LISTING_LAUNCH"

    # If listing starts inside window, confirm first Bybit-available minute.
    if hist is not None and base > requested_start:
        first = first_available_bybit_open(
            hist, symbol, start=base, end=REQUESTED_END, probe_hours=6
        )
        if first is not None and first > base:
            return first, "LISTING_FIRST_BYBIT_AVAILABLE"
        if first is None:
            # Could not confirm; keep launch/requested base
            return base, src
        return first, src if src.startswith("LISTING") else "LISTING_FIRST_BYBIT_AVAILABLE"

    return base, src


def classify_coverage(
    *,
    expected_start: datetime | None,
    actual_min: datetime | None,
    missing_minutes: int,
    requested_start: datetime,
) -> str:
    if expected_start is None:
        return "UNKNOWN_START"
    if actual_min is None:
        return "INCOMPLETE"
    if missing_minutes > 0:
        return "INCOMPLETE"
    if ensure_utc(expected_start) <= ensure_utc(requested_start):
        return "FULL_REQUESTED_WINDOW"
    return "LISTING_LIMITED_COMPLETE"


def coin_verdict(
    *,
    coverage_class: str,
    missing_minutes: int,
    longest_gap_minutes: int,
    invalid_ohlc: int,
    off_grid: int,
    physical_extra: int,
    negative_volume: int,
) -> str:
    if invalid_ohlc > 0 or off_grid > 0 or negative_volume > 0:
        return "DATA_INTEGRITY_FAILURE"
    if coverage_class == "INCOMPLETE" or coverage_class == "UNKNOWN_START":
        if (
            missing_minutes <= MINOR_GAP_MAX_MISSING
            and longest_gap_minutes <= MINOR_GAP_MAX_LONGEST
            and missing_minutes > 0
        ):
            return "MINOR_GAPS"
        return "INCOMPLETE"
    if coverage_class == "LISTING_LIMITED_COMPLETE":
        if physical_extra > 0:
            return "COMPLETE_WITH_PHYSICAL_DUPLICATES"
        return "LISTING_LIMITED_CLEAN"
    # FULL
    if physical_extra > 0:
        return "COMPLETE_WITH_PHYSICAL_DUPLICATES"
    return "COMPLETE_CLEAN"


def gap_ranges_sql(
    client: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Return contiguous missing ranges inside [start, end) using FINAL series + leading/trailing."""
    table = f"{client.database}.candles_1m"
    # Internal gaps
    q = client.query(
        f"""
        SELECT
            prev_ot + toIntervalMinute(1) AS gap_start,
            open_time AS gap_end_exclusive,
            intDiv(dateDiff('second', prev_ot, open_time), 60) - 1 AS missing_minutes
        FROM (
            SELECT
                open_time,
                lagInFrame(open_time, 1, open_time) OVER (
                    ORDER BY open_time
                    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
                ) AS prev_ot
            FROM {table} FINAL
            WHERE exchange = 'bybit'
              AND symbol = {{symbol:String}}
              AND interval = '1m'
              AND is_closed = 1
              AND open_time >= {{start:DateTime64(3, 'UTC')}}
              AND open_time < {{end:DateTime64(3, 'UTC')}}
        )
        WHERE open_time > prev_ot
          AND dateDiff('second', prev_ot, open_time) != 60
        ORDER BY gap_start
        """,
        parameters={"symbol": symbol, "start": start, "end": end},
    )
    rows = []
    for gap_start, gap_end, missing in q.result_rows:
        rows.append(
            {
                "symbol": symbol,
                "gap_start": _iso(ensure_utc(gap_start)),
                "gap_end": _iso(ensure_utc(gap_end)),  # exclusive next present
                "missing_minutes": int(missing),
                "gap_kind": "INTERNAL",
            }
        )
    return rows


def audit_symbol(
    client: Any,
    *,
    symbol: str,
    expected_start: datetime | None,
    start_source: str,
    requested_start: datetime,
    requested_end: datetime,
) -> dict[str, Any]:
    table = f"{client.database}.candles_1m"
    # Scan entire requested window for actual min/max; expected window for coverage.
    win_start = requested_start
    win_end = requested_end

    agg = client.query(
        f"""
        SELECT
            count() AS unique_final,
            min(open_time) AS min_ot,
            max(open_time) AS max_ot,
            countIf(
                open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
                OR isNaN(toFloat64(open)) OR isNaN(toFloat64(high))
                OR isNaN(toFloat64(low)) OR isNaN(toFloat64(close))
            ) AS null_ohlc,
            countIf(
                open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
                OR high < greatest(open, close)
                OR low > least(open, close)
                OR high < low
            ) AS invalid_ohlc,
            countIf(volume < 0) AS negative_volume,
            countIf(turnover < 0) AS negative_turnover,
            countIf(volume = 0) AS zero_volume,
            countIf(toUnixTimestamp64Milli(open_time) % 60000 != 0) AS off_grid,
            countIf(close_time != open_time + toIntervalMinute(1)) AS close_time_errors,
            countIf(open_time >= {{end:DateTime64(3, 'UTC')}}) AS out_of_window_ge_end
        FROM {table} FINAL
        WHERE exchange = 'bybit'
          AND symbol = {{symbol:String}}
          AND interval = '1m'
          AND is_closed = 1
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        """,
        parameters={"symbol": symbol, "start": win_start, "end": win_end},
    )
    (
        unique_in_window,
        min_ot,
        max_ot,
        null_ohlc,
        invalid_ohlc,
        neg_vol,
        neg_to,
        zero_vol,
        off_grid,
        close_err,
        out_ge_end,
    ) = agg.result_rows[0]

    unique_in_window = int(unique_in_window)
    null_ohlc = int(null_ohlc)
    invalid_ohlc = int(invalid_ohlc)
    neg_vol = int(neg_vol)
    neg_to = int(neg_to)
    zero_vol = int(zero_vol)
    off_grid = int(off_grid)
    close_err = int(close_err)
    out_ge_end = int(out_ge_end)
    if min_ot is not None:
        min_ot = ensure_utc(min_ot)
    if max_ot is not None:
        max_ot = ensure_utc(max_ot)

    phys = client.query(
        f"""
        SELECT count(), uniqExact(open_time)
        FROM {table}
        WHERE exchange = 'bybit'
          AND symbol = {{symbol:String}}
          AND interval = '1m'
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        """,
        parameters={"symbol": symbol, "start": win_start, "end": win_end},
    )
    physical_rows, uniq_exact_physical = (int(phys.result_rows[0][0]), int(phys.result_rows[0][1]))

    # Early rows before expected start (unexpected pre-start)
    early = 0
    if expected_start is not None:
        er = client.query(
            f"""
            SELECT count()
            FROM {table} FINAL
            WHERE exchange = 'bybit' AND symbol = {{symbol:String}} AND interval = '1m'
              AND is_closed = 1
              AND open_time >= {{ws:DateTime64(3, 'UTC')}}
              AND open_time < {{es:DateTime64(3, 'UTC')}}
            """,
            parameters={"symbol": symbol, "ws": win_start, "es": expected_start},
        )
        early = int(er.result_rows[0][0])

    # Coverage evaluation window
    if expected_start is None:
        cov_start = min_ot  # best effort
        expected_minutes = None
    else:
        cov_start = expected_start
        expected_minutes = expected_candle_count(cov_start, win_end)

    # Unique minutes in expected coverage window
    if cov_start is not None:
        cr = client.query(
            f"""
            SELECT count(), min(open_time), max(open_time)
            FROM {table} FINAL
            WHERE exchange = 'bybit' AND symbol = {{symbol:String}} AND interval = '1m'
              AND is_closed = 1
              AND open_time >= {{start:DateTime64(3, 'UTC')}}
              AND open_time < {{end:DateTime64(3, 'UTC')}}
            """,
            parameters={"symbol": symbol, "start": cov_start, "end": win_end},
        )
        actual_unique = int(cr.result_rows[0][0])
        cov_min = cr.result_rows[0][1]
        cov_max = cr.result_rows[0][2]
        if cov_min is not None:
            cov_min = ensure_utc(cov_min)
        if cov_max is not None:
            cov_max = ensure_utc(cov_max)
    else:
        actual_unique = unique_in_window
        cov_min, cov_max = min_ot, max_ot

    # Gaps (internal) + leading/trailing relative to expected window
    gaps = []
    if cov_start is not None and actual_unique > 0:
        gaps = gap_ranges_sql(client, symbol=symbol, start=cov_start, end=win_end)
        # Leading gap: actual min > expected start
        if cov_min is not None and cov_min > cov_start:
            missing_lead = int((cov_min - cov_start).total_seconds() // 60)
            if missing_lead > 0:
                gaps.insert(
                    0,
                    {
                        "symbol": symbol,
                        "gap_start": _iso(cov_start),
                        "gap_end": _iso(cov_min),
                        "missing_minutes": missing_lead,
                        "gap_kind": "LEADING",
                    },
                )
        # Trailing: max < end-1m
        last_expected = win_end - timedelta(minutes=1)
        if cov_max is not None and cov_max < last_expected:
            missing_trail = int((last_expected - cov_max).total_seconds() // 60)
            if missing_trail > 0:
                gaps.append(
                    {
                        "symbol": symbol,
                        "gap_start": _iso(cov_max + timedelta(minutes=1)),
                        "gap_end": _iso(win_end),
                        "missing_minutes": missing_trail,
                        "gap_kind": "TRAILING",
                    },
                )

    missing_minutes = int(sum(g["missing_minutes"] for g in gaps))
    if expected_minutes is not None:
        # Prefer exact arithmetic when continuous expectation known
        missing_by_count = max(expected_minutes - actual_unique, 0)
        # Use max to avoid under-reporting if gap SQL missed something
        missing_minutes = max(missing_minutes, missing_by_count)

    gap_count = len(gaps)
    longest_gap = max((g["missing_minutes"] for g in gaps), default=0)

    duplicate_minutes = max(physical_rows - uniq_exact_physical, 0)
    # logical unique should match FINAL unique_in_window; extra physical versions:
    physical_extra = max(physical_rows - unique_in_window, 0)

    coverage_class = classify_coverage(
        expected_start=expected_start,
        actual_min=min_ot,
        missing_minutes=missing_minutes,
        requested_start=requested_start,
    )

    verdict = coin_verdict(
        coverage_class=coverage_class,
        missing_minutes=missing_minutes,
        longest_gap_minutes=longest_gap,
        invalid_ohlc=invalid_ohlc + null_ohlc,
        off_grid=off_grid,
        physical_extra=physical_extra,
        negative_volume=neg_vol + neg_to,
    )

    zero_rate = (100.0 * zero_vol / actual_unique) if actual_unique else None

    # Aggregation readiness (15m / 30m / 1h / 4h) via SQL grouping
    agg_stats: dict[str, Any] = {}
    if cov_start is not None:
        for tf, mins in (("15m", 15), ("30m", 30), ("1h", 60), ("4h", 240)):
            # Align cov_start up to bucket boundary
            bs = bucket_start(cov_start, tf)
            if bs < cov_start:
                bs = bs + timedelta(minutes=mins)
            # last complete bucket must close by win_end
            # bucket_open + mins <= win_end  =>  bucket_open < win_end - (mins-?); close == open+mins
            # complete if open + mins <= win_end and all 1m present
            aq = client.query(
                f"""
                SELECT
                    countIf(c = {{need:UInt32}}) AS complete_buckets,
                    countIf(c > 0 AND c < {{need:UInt32}}) AS incomplete_buckets,
                    minIf(b, c = {{need:UInt32}}) AS first_complete,
                    maxIf(b, c = {{need:UInt32}}) AS last_complete
                FROM (
                    SELECT
                        toStartOfInterval(open_time, INTERVAL {{mins:UInt32}} minute) AS b,
                        count() AS c
                    FROM {table} FINAL
                    WHERE exchange = 'bybit'
                      AND symbol = {{symbol:String}}
                      AND interval = '1m'
                      AND is_closed = 1
                      AND open_time >= {{start:DateTime64(3, 'UTC')}}
                      AND open_time < {{end:DateTime64(3, 'UTC')}}
                    GROUP BY b
                )
                WHERE b >= {{bstart:DateTime64(3, 'UTC')}}
                  AND b + toIntervalMinute({{mins:UInt32}}) <= {{end:DateTime64(3, 'UTC')}}
                """,
                parameters={
                    "symbol": symbol,
                    "start": cov_start,
                    "end": win_end,
                    "bstart": bs,
                    "mins": mins,
                    "need": mins,
                },
            )
            row = aq.result_rows[0]
            first_c = row[2]
            last_c = row[3]
            agg_stats[tf] = {
                "complete_buckets": int(row[0] or 0),
                "incomplete_buckets": int(row[1] or 0),
                "first_complete": _iso(ensure_utc(first_c)) if first_c and getattr(first_c, "year", 1971) > 1970 else None,
                "last_complete": _iso(ensure_utc(last_c)) if last_c and getattr(last_c, "year", 1971) > 1970 else None,
                "expected_1m_per_bucket": mins,
            }

    return {
        "symbol": symbol,
        "expected_start": _iso(expected_start),
        "expected_start_source": start_source,
        "expected_end": _iso(requested_end),
        "actual_min_ts": _iso(min_ot),
        "actual_max_ts": _iso(max_ot),
        "expected_minutes": expected_minutes,
        "actual_unique_minutes": actual_unique,
        "missing_minutes": missing_minutes,
        "missing_ratio": (
            round(missing_minutes / expected_minutes, 6)
            if expected_minutes
            else None
        ),
        "physical_rows": physical_rows,
        "unique_minutes_final_window": unique_in_window,
        "uniq_exact_physical": uniq_exact_physical,
        "extra_duplicate_rows": physical_extra,
        "duplicate_timestamp_count": duplicate_minutes,
        "duplicate_class": (
            "PHYSICAL_DUPLICATES_EXPECTED_BY_ENGINE"
            if physical_extra > 0 and missing_minutes == 0
            else ("LOGICAL_DUPLICATE_PROBLEM" if duplicate_minutes > 0 and unique_in_window < uniq_exact_physical else ("NONE" if physical_extra == 0 else "PHYSICAL_DUPLICATES_EXPECTED_BY_ENGINE"))
        ),
        "gap_count": gap_count,
        "longest_gap_minutes": longest_gap,
        "coverage_class": coverage_class,
        "verdict": verdict,
        "null_ohlc_rows": null_ohlc,
        "invalid_ohlc_rows": invalid_ohlc,
        "off_grid_rows": off_grid,
        "close_time_errors": close_err,
        "out_of_window_rows": out_ge_end,
        "early_pre_expected_rows": early,
        "zero_volume_count": zero_vol,
        "zero_volume_rate": round(zero_rate, 4) if zero_rate is not None else None,
        "negative_volume_count": neg_vol,
        "negative_turnover_count": neg_to,
        "gaps": gaps,
        "aggregation": agg_stats,
    }


def bybit_spot_checks(
    client: Any,
    symbols: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Compare a few CH candles vs Bybit API for selected symbols."""
    repo = CandleRepository(client)
    hist = BybitHistoryClient()
    rows_out: list[dict[str, Any]] = []

    # Fixed sample minutes inside coverage where possible
    sample_times = [
        datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 6, 15, 18, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc),
    ]
    # Also try Dec for majors (may be missing in CH)
    sample_times_dec = [
        datetime(2025, 12, 15, 12, 0, tzinfo=timezone.utc),
    ]

    for symbol in symbols:
        times = list(sample_times)
        if symbol in ("BTCUSDT", "DOGEUSDT", "APTUSDT", "ETHUSDT"):
            times = sample_times_dec + times
        for ts in times:
            if ts >= REQUESTED_END or ts < REQUESTED_START:
                continue
            ch_rows = repo.get_candles(symbol, ts, ts + timedelta(minutes=1))
            ch = ch_rows[0] if ch_rows else None
            bybit_err = None
            bybit_c = None
            try:
                fetched = hist.fetch_closed_1m(symbol, ts, ts + timedelta(minutes=1))
                bybit_c = fetched[0] if fetched else None
            except Exception as exc:  # noqa: BLE001
                bybit_err = str(exc)

            def _dec(x: Any) -> str | None:
                if x is None:
                    return None
                return str(Decimal(str(x)))

            fields = ["open", "high", "low", "close", "volume"]
            if bybit_c is None and bybit_err:
                rows_out.append(
                    {
                        "symbol": symbol,
                        "open_time": _iso(ts),
                        "ch_present": ch is not None,
                        "bybit_present": False,
                        "bybit_error": bybit_err,
                        "all_match": False,
                        "note": "bybit_unreachable_or_empty",
                    }
                )
                continue
            if bybit_c is None:
                rows_out.append(
                    {
                        "symbol": symbol,
                        "open_time": _iso(ts),
                        "ch_present": ch is not None,
                        "bybit_present": False,
                        "all_match": False,
                        "note": "bybit_empty",
                    }
                )
                continue
            if ch is None:
                rows_out.append(
                    {
                        "symbol": symbol,
                        "open_time": _iso(ts),
                        "ch_present": False,
                        "bybit_present": True,
                        "bybit_open": _dec(bybit_c.open),
                        "bybit_high": _dec(bybit_c.high),
                        "bybit_low": _dec(bybit_c.low),
                        "bybit_close": _dec(bybit_c.close),
                        "bybit_volume": _dec(bybit_c.volume),
                        "all_match": False,
                        "note": "missing_in_clickhouse",
                    }
                )
                continue
            matches = {}
            all_ok = True
            for f in fields:
                bv = _dec(getattr(bybit_c, f))
                cv = _dec(ch.get(f))
                ok = bv is not None and cv is not None and Decimal(bv) == Decimal(cv)
                matches[f] = ok
                if not ok:
                    all_ok = False
            rows_out.append(
                {
                    "symbol": symbol,
                    "open_time": _iso(ts),
                    "ch_present": True,
                    "bybit_present": True,
                    "bybit_open": _dec(bybit_c.open),
                    "ch_open": _dec(ch.get("open")),
                    "bybit_high": _dec(bybit_c.high),
                    "ch_high": _dec(ch.get("high")),
                    "bybit_low": _dec(bybit_c.low),
                    "ch_low": _dec(ch.get("low")),
                    "bybit_close": _dec(bybit_c.close),
                    "ch_close": _dec(ch.get("close")),
                    "bybit_volume": _dec(bybit_c.volume),
                    "ch_volume": _dec(ch.get("volume")),
                    "match_open": matches["open"],
                    "match_high": matches["high"],
                    "match_low": matches["low"],
                    "match_close": matches["close"],
                    "match_volume": matches["volume"],
                    "all_match": all_ok,
                    "note": "ok" if all_ok else "value_mismatch",
                }
            )
    return rows_out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    symbols = load_universe()
    log_map = parse_backfill_log(LOG_PATH) if LOG_PATH.exists() else {}

    client = get_client()
    version, db, user = client.ping()

    # Document table semantics
    create_sql = client.query("SHOW CREATE TABLE signal_generator.candles_1m").result_rows[0][0]

    # Universe integrity vs CH
    ch_syms_q = client.query(
        f"""
        SELECT DISTINCT symbol
        FROM {client.database}.candles_1m
        WHERE exchange = 'bybit' AND interval = '1m'
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        """,
        parameters={"start": REQUESTED_START, "end": REQUESTED_END},
    )
    ch_all = {str(r[0]).upper() for r in ch_syms_q.result_rows}
    universe_set = set(symbols)
    missing_symbols = sorted(universe_set - ch_all)
    # unexpected among those that appear heavily? report CH symbols in universe window not in universe
    unexpected = sorted(ch_all - universe_set)

    print(f"Universe={len(symbols)} CH_found_in_window={len(universe_set & ch_all)} missing={missing_symbols}", flush=True)

    print("Fetching Bybit instrument details…", flush=True)
    details = fetch_instrument_details(symbols)
    hist_client = BybitHistoryClient()

    coverage_rows: list[dict[str, Any]] = []
    gap_rows: list[dict[str, Any]] = []
    dup_rows: list[dict[str, Any]] = []
    ohlc_rows: list[dict[str, Any]] = []
    vol_rows: list[dict[str, Any]] = []
    ts_rows: list[dict[str, Any]] = []
    agg_rows: list[dict[str, Any]] = []
    log_cmp: list[dict[str, Any]] = []

    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym}…", flush=True)
        detail = details.get(sym)
        exp_start, start_src = expected_start_for(
            sym, detail, requested_start=REQUESTED_START, hist=hist_client
        )
        # Validate instrument is linear USDT perpetual when available
        instrument_ok = True
        if detail and not detail.get("_error"):
            instrument_ok = (
                detail.get("settleCoin") == "USDT"
                and detail.get("contractType") == "LinearPerpetual"
                and detail.get("status") == "Trading"
            )

        rep = audit_symbol(
            client,
            symbol=sym,
            expected_start=exp_start,
            start_source=start_src,
            requested_start=REQUESTED_START,
            requested_end=REQUESTED_END,
        )
        rep["instrument_ok"] = instrument_ok
        if detail and not detail.get("_error"):
            lt = detail.get("launchTime")
            rep["launch_time"] = (
                _iso(millis_to_utc(lt)) if lt not in (None, "", "0") else None
            )
            rep["contract_type"] = detail.get("contractType")
            rep["instrument_status"] = detail.get("status")
        else:
            rep["launch_time"] = None
            rep["contract_type"] = None
            rep["instrument_status"] = None
            if detail and detail.get("_error"):
                rep["instrument_error"] = detail["_error"]

        coverage_rows.append({k: v for k, v in rep.items() if k not in ("gaps", "aggregation")})
        for g in rep["gaps"]:
            gap_rows.append(g)
        dup_rows.append(
            {
                "symbol": sym,
                "physical_rows": rep["physical_rows"],
                "unique_minutes": rep["actual_unique_minutes"],
                "unique_final_in_requested_window": rep["unique_minutes_final_window"],
                "extra_duplicate_rows": rep["extra_duplicate_rows"],
                "duplicate_timestamp_count": rep["duplicate_timestamp_count"],
                "duplicate_class": rep["duplicate_class"],
            }
        )
        ohlc_rows.append(
            {
                "symbol": sym,
                "null_ohlc_rows": rep["null_ohlc_rows"],
                "invalid_ohlc_rows": rep["invalid_ohlc_rows"],
            }
        )
        vol_rows.append(
            {
                "symbol": sym,
                "zero_volume_count": rep["zero_volume_count"],
                "zero_volume_rate": rep["zero_volume_rate"],
                "negative_volume_count": rep["negative_volume_count"],
                "negative_turnover_count": rep["negative_turnover_count"],
                "flag_high_zero_volume": bool(
                    rep["zero_volume_rate"] is not None and rep["zero_volume_rate"] >= 5.0
                ),
            }
        )
        ts_rows.append(
            {
                "symbol": sym,
                "off_grid_rows": rep["off_grid_rows"],
                "close_time_errors": rep["close_time_errors"],
                "out_of_window_rows": rep["out_of_window_rows"],
                "early_pre_expected_rows": rep["early_pre_expected_rows"],
            }
        )
        for tf, st in rep["aggregation"].items():
            agg_rows.append({"symbol": sym, "timeframe": tf, **st})

        lg = log_map.get(sym, {})
        ch_u = rep["actual_unique_minutes"]
        ch_e = rep["expected_minutes"]
        match = (
            lg.get("log_final_actual") == ch_u
            and lg.get("log_final_expected") == ch_e
            and lg.get("log_gaps") == rep["gap_count"]
        ) if lg else False
        # Note: prior backfill quality used min_ot as effective start for some coins,
        # so log expected may equal Jan-1 window while true expected is full window.
        log_cmp.append(
            {
                "symbol": sym,
                "log_final_actual": lg.get("log_final_actual"),
                "log_final_expected": lg.get("log_final_expected"),
                "log_gaps": lg.get("log_gaps"),
                "log_quality": lg.get("log_quality"),
                "ch_unique_minutes": ch_u,
                "ch_expected_minutes": ch_e,
                "ch_gap_count": rep["gap_count"],
                "ch_missing_minutes": rep["missing_minutes"],
                "match_log_counts": match,
                "mismatch_reason": (
                    None
                    if match
                    else (
                        "LOG_USED_MIN_OPEN_AS_EXPECTED"
                        if lg
                        and lg.get("log_final_actual") == ch_u
                        and lg.get("log_final_expected") == lg.get("log_final_actual")
                        and ch_e is not None
                        and ch_e != lg.get("log_final_expected")
                        else ("NO_LOG" if not lg else "COUNT_MISMATCH")
                    )
                ),
            }
        )

    # Also: for Jan-1 coins, confirm Bybit HAS Dec data (true incompleteness)
    print("Confirming Dec availability for Jan-1-start symbols…", flush=True)
    for r in coverage_rows:
        if r["actual_min_ts"] and str(r["actual_min_ts"]).startswith("2026-01-01"):
            try:
                probe = hist_client.fetch_closed_1m(
                    r["symbol"],
                    REQUESTED_START,
                    REQUESTED_START + timedelta(hours=2),
                    max_pages=2,
                )
                r["bybit_has_requested_start"] = len(probe) > 0
                if probe:
                    r["bybit_first_in_dec_probe"] = _iso(probe[0].open_time)
            except Exception as exc:  # noqa: BLE001
                r["bybit_has_requested_start"] = None
                r["bybit_dec_probe_error"] = str(exc)

    print("Bybit spot checks…", flush=True)
    spot = bybit_spot_checks(client, SPOT_SYMBOLS)

    # Count classes
    cov_counts = Counter(r["coverage_class"] for r in coverage_rows)
    verd_counts = Counter(r["verdict"] for r in coverage_rows)
    incomplete = [r for r in coverage_rows if r["coverage_class"] == "INCOMPLETE"]
    with_dups = [r for r in coverage_rows if (r["extra_duplicate_rows"] or 0) > 0]
    ohlc_viol = [r for r in coverage_rows if (r["invalid_ohlc_rows"] or 0) + (r["null_ohlc_rows"] or 0) > 0]
    ts_viol = [r for r in coverage_rows if (r["off_grid_rows"] or 0) > 0 or (r["close_time_errors"] or 0) > 0]
    log_mis = [r for r in log_cmp if not r["match_log_counts"]]

    longest = max(coverage_rows, key=lambda r: r["longest_gap_minutes"] or 0) if coverage_rows else None

    # Explain count buckets
    full_expected = expected_candle_count(REQUESTED_START, REQUESTED_END)  # 349920
    jan1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    jan1_expected = expected_candle_count(jan1, REQUESTED_END)  # 319680
    lit = next((r for r in coverage_rows if r["symbol"] == "LITUSDT"), None)

    count_explanation = {
        "349920": {
            "meaning": "Full requested window minutes",
            "formula": "expected_candle_count(2025-12-11, 2026-08-11)",
            "value": full_expected,
            "symbols_with_this_ch_unique_when_complete": [
                r["symbol"]
                for r in coverage_rows
                if r["actual_unique_minutes"] == full_expected and r["missing_minutes"] == 0
            ],
        },
        "319680": {
            "meaning": "Jan 1 2026 → Aug 11 2026 only (missing Dec 11–31)",
            "formula": "expected_candle_count(2026-01-01, 2026-08-11)",
            "value": jan1_expected,
            "delta_vs_full": full_expected - jan1_expected,
            "symbols_with_this_unique": [
                r["symbol"] for r in coverage_rows if r["actual_unique_minutes"] == jan1_expected
            ],
            "note": (
                "These coins are NOT listing-limited: Bybit has Dec-2025 history "
                "(e.g. BTCUSDT). ClickHouse simply lacks Dec 11–31 for them. "
                "Prior backfill log marked them COMPLETE by treating min(open_time) "
                "as effective_start."
            ),
        },
        "321732": {
            "meaning": "LITUSDT listing-limited from launch ceil to end",
            "value": lit["actual_unique_minutes"] if lit else None,
            "expected_start": lit["expected_start"] if lit else None,
            "launch_time": lit.get("launch_time") if lit else None,
        },
    }

    # Primary decision
    integrity_fail = any(r["verdict"] == "DATA_INTEGRITY_FAILURE" for r in coverage_rows)
    needs_repair = any(r["verdict"] == "INCOMPLETE" for r in coverage_rows)
    minor_only = (
        not integrity_fail
        and not needs_repair
        and any(r["verdict"] == "MINOR_GAPS" for r in coverage_rows)
    )
    if integrity_fail:
        primary = "CLICKHOUSE_51_COIN_HISTORY_INVALID"
        recommendation = "Do not start multi-coin backtests until integrity failures are repaired."
    elif needs_repair:
        primary = "CLICKHOUSE_51_COIN_HISTORY_NEEDS_REPAIR"
        recommendation = (
            "Repair leading Dec-2025 gaps for Jan-1-start majors via targeted "
            "`--repair-missing` / backfill of [2025-12-11, 2026-01-01) before treating "
            "the 51-coin set as full-window ready. No mutation performed in this audit."
        )
    elif minor_only:
        primary = "CLICKHOUSE_51_COIN_HISTORY_READY_WITH_MINOR_GAPS"
        recommendation = "Backtests possible; document minor gaps."
    else:
        primary = "CLICKHOUSE_51_COIN_HISTORY_READY"
        recommendation = "History ready for backtests on the audited window."

    ready = [
        r["symbol"]
        for r in coverage_rows
        if r["verdict"] in ("COMPLETE_CLEAN", "LISTING_LIMITED_CLEAN", "COMPLETE_WITH_PHYSICAL_DUPLICATES")
        and r["coverage_class"] in ("FULL_REQUESTED_WINDOW", "LISTING_LIMITED_COMPLETE")
    ]
    not_ready = [r["symbol"] for r in coverage_rows if r["symbol"] not in ready]

    spot_pass = all(r.get("all_match") for r in spot if r.get("ch_present") and r.get("bybit_present"))
    spot_missing_ch = [r for r in spot if r.get("note") == "missing_in_clickhouse"]

    # Write artifacts
    write_csv(OUT / "coverage_by_symbol.csv", coverage_rows)
    write_csv(OUT / "log_vs_clickhouse.csv", log_cmp)
    write_csv(OUT / "gap_ranges.csv", gap_rows)
    write_csv(OUT / "duplicate_audit.csv", dup_rows)
    write_csv(OUT / "ohlc_integrity.csv", ohlc_rows)
    write_csv(OUT / "volume_integrity.csv", vol_rows)
    write_csv(OUT / "timestamp_integrity.csv", ts_rows)
    write_csv(OUT / "aggregation_readiness.csv", agg_rows)
    write_csv(OUT / "bybit_spot_checks.csv", spot)

    meta = {
        "task": "AUDIT_CLICKHOUSE_51_COIN_HISTORY_INTEGRITY",
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "clickhouse": {
            "host_version": version,
            "database": db,
            "user": user,
            "table": "candles_1m",
            "engine": "ReplacingMergeTree(ingested_at)",
            "order_by": "(exchange, symbol, interval, open_time)",
            "partition_by": "toYYYYMM(open_time)",
            "timestamp_field": "open_time",
            "symbol_field": "symbol",
            "ohlcv_fields": ["open", "high", "low", "close", "volume", "turnover"],
            "dedup_semantics": "FINAL required for logical uniqueness; physical duplicates expected until merge",
            "create_table_sql_prefix": create_sql[:500],
        },
        "requested_window": {
            "start": _iso(REQUESTED_START),
            "end_exclusive": _iso(REQUESTED_END),
            "full_expected_minutes": full_expected,
        },
        "universe": {
            "path": str(UNIVERSE_PATH),
            "UNIVERSE_COUNT": len(symbols),
            "duplicates_in_universe": len(symbols) - len(set(symbols)),
            "CH_SYMBOLS_FOUND": len(universe_set & ch_all),
            "MISSING_SYMBOLS": missing_symbols,
            "UNEXPECTED_SYMBOLS_IN_WINDOW": unexpected[:50],
            "unexpected_count": len(unexpected),
        },
        "coverage_counts": dict(cov_counts),
        "verdict_counts": dict(verd_counts),
        "count_bucket_explanation": count_explanation,
        "primary_decision": primary,
        "recommendation": recommendation,
        "ready_for_backtest": ready,
        "not_ready_for_backtest": not_ready,
        "mutations": "NONE",
        "strategy_logic_changed": "NO",
    }
    (OUT / "audit_metadata.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")

    # summary.md
    lines = [
        "# AUDIT_CLICKHOUSE_51_COIN_HISTORY_INTEGRITY",
        "",
        f"Primary: `{primary}`",
        "",
        "## Source of truth",
        "",
        f"- Table: `{db}.candles_1m`",
        "- Engine: `ReplacingMergeTree(ingested_at)`",
        "- ORDER BY: `(exchange, symbol, interval, open_time)`",
        "- Timestamp: `open_time` (DateTime64(3,'UTC'))",
        "- Audit reads: **FINAL** (logical dedup)",
        f"- Window: `{_iso(REQUESTED_START)}` → `{_iso(REQUESTED_END)}` (end exclusive)",
        "",
        "## Universe",
        "",
        f"- UNIVERSE_COUNT: **{len(symbols)}**",
        f"- CH_SYMBOLS_FOUND: **{len(universe_set & ch_all)}**",
        f"- MISSING_SYMBOLS: {missing_symbols or '[]'}",
        f"- UNEXPECTED_SYMBOLS (in window, not in universe): {len(unexpected)}",
        "",
        "## Coverage classes",
        "",
    ]
    for k, v in sorted(cov_counts.items()):
        lines.append(f"- {k}: **{v}**")
    lines += ["", "## Verdicts", ""]
    for k, v in sorted(verd_counts.items()):
        lines.append(f"- {k}: **{v}**")
    lines += [
        "",
        "## Count buckets explained",
        "",
        f"- **349920** = full window minutes ({full_expected})",
        f"- **319680** = Jan1→Aug11 only ({jan1_expected}); missing Dec11–31 = {full_expected - jan1_expected} minutes",
        f"- **321732** = LITUSDT listing-limited (start {lit['expected_start'] if lit else '—'})",
        "",
        "### Incomplete / not full-window",
        "",
    ]
    for r in incomplete:
        lines.append(
            f"- `{r['symbol']}`: unique={r['actual_unique_minutes']} expected={r['expected_minutes']} "
            f"missing={r['missing_minutes']} actual_min={r['actual_min_ts']} expected_start={r['expected_start']}"
        )
    lines += [
        "",
        f"## Longest gap: {longest['symbol'] if longest else '—'} ({longest['longest_gap_minutes'] if longest else 0} minutes)",
        f"## OHLC violations: {len(ohlc_viol)} symbols",
        f"## Timestamp violations: {len(ts_viol)} symbols",
        f"## Physical-dup symbols: {len(with_dups)}",
        f"## Log-vs-DB mismatches: {len(log_mis)}",
        f"## Bybit spot-check: present-match PASS={spot_pass}; CH-missing samples={len(spot_missing_ch)}",
        "",
        f"## Ready ({len(ready)}): {', '.join(ready[:20])}{'…' if len(ready)>20 else ''}",
        f"## Not ready ({len(not_ready)}): {', '.join(not_ready)}",
        "",
        "## Recommendation",
        "",
        recommendation,
        "",
        "## Safety",
        "",
        "- Mutations: NONE",
        "- Strategy logic changed: NO",
        "",
    ]
    (OUT / "summary.md").write_text("\n".join(lines), encoding="utf-8")

    print(f"PRIMARY {primary}")
    print(f"FULL={cov_counts.get('FULL_REQUESTED_WINDOW',0)} LISTING={cov_counts.get('LISTING_LIMITED_COMPLETE',0)} INCOMPLETE={cov_counts.get('INCOMPLETE',0)}")
    print(f"ready={len(ready)} not_ready={len(not_ready)}")
    print(f"wrote {OUT}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
