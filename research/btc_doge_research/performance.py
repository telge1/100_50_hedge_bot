"""Bounded storage and query benchmarks on pilot data only."""

from __future__ import annotations

from statistics import median
from time import perf_counter
from typing import Any

from .clickhouse import rows
from .contracts import TARGET_DATABASE


def _measure(client: Any, name: str, sql: str) -> dict[str, Any]:
    timings = []
    summaries = []
    result_rows = 0
    for _ in range(3):
        started = perf_counter()
        result = client.query(sql)
        timings.append((perf_counter() - started) * 1000)
        result_rows = len(result.result_rows)
        summaries.append(getattr(result, "summary", {}) or {})
    summary = summaries[-1]
    return {
        "query_name": name,
        "cold_or_first_run": round(timings[0], 3),
        "warm_run": round(median(timings[1:]), 3),
        "rows_read": int(summary.get("read_rows", 0) or 0),
        "bytes_read": int(summary.get("read_bytes", 0) or 0),
        "elapsed_ms": round(median(timings), 3),
        "result_rows": result_rows,
        "uses_final": False,
    }


def run_benchmarks(client: Any) -> list[dict[str, Any]]:
    db = TARGET_DATABASE
    scenarios = {
        "single_timestamp_ob200": f"""
            SELECT event_time,bid_prices,bid_sizes,ask_prices,ask_sizes
            FROM {db}.research_orderbook_ob200_snapshots
            WHERE symbol='BTCUSDT'
            ORDER BY abs(dateDiff('millisecond', event_time,
                toDateTime64('2026-08-31 19:00:00',3,'UTC')))
            LIMIT 1
        """,
        "ob200_plus_minus_5m": f"""
            SELECT count(),sum(length(bid_prices)+length(ask_prices))
            FROM {db}.research_orderbook_ob200_snapshots
            WHERE symbol='BTCUSDT'
              AND event_time >= toDateTime64('2026-08-31 18:55:00',3,'UTC')
              AND event_time < toDateTime64('2026-08-31 19:05:00',3,'UTC')
        """,
        "ob200_plus_minus_30m": f"""
            SELECT count(),sum(length(bid_prices)+length(ask_prices))
            FROM {db}.research_orderbook_ob200_snapshots
            WHERE symbol='BTCUSDT'
              AND event_time >= toDateTime64('2026-08-31 18:30:00',3,'UTC')
              AND event_time < toDateTime64('2026-08-31 19:30:00',3,'UTC')
        """,
        "ob200_one_hour": f"""
            SELECT count(),sum(length(bid_prices)+length(ask_prices))
            FROM {db}.research_orderbook_ob200_snapshots
            WHERE symbol='DOGEUSDT'
              AND event_time >= toDateTime64('2026-08-29 11:45:00',3,'UTC')
              AND event_time < toDateTime64('2026-08-29 12:30:00',3,'UTC')
        """,
        "btc_1m": f"SELECT * FROM {db}.research_market_1m WHERE symbol='BTCUSDT' ORDER BY bucket_time",
        "btc_1s": f"SELECT * FROM {db}.research_market_1s WHERE symbol='BTCUSDT' ORDER BY bucket_time",
        "doge_1m": f"SELECT * FROM {db}.research_market_1m WHERE symbol='DOGEUSDT' ORDER BY bucket_time",
        "doge_1s": f"SELECT * FROM {db}.research_market_1s WHERE symbol='DOGEUSDT' ORDER BY bucket_time",
        "trade_events": f"SELECT count(),sum(base_size),sum(quote_notional) FROM {db}.research_public_trades WHERE symbol='BTCUSDT'",
        "liquidations": f"SELECT * FROM {db}.research_liquidation_events WHERE symbol='BTCUSDT' ORDER BY event_time",
        "orderbook_1s": f"SELECT * FROM {db}.research_orderbook_1s WHERE symbol='BTCUSDT' ORDER BY bucket_time",
        "pool_wall_near_levels": f"""
            SELECT event_time,
                   arrayFilter(x -> abs(x.1 - (bid_prices[1]+ask_prices[1])/2)
                     / ((bid_prices[1]+ask_prices[1])/2) * 10000 <= 50,
                     arrayZip(bid_prices,bid_sizes)) AS near_bids,
                   arrayFilter(x -> abs(x.1 - (bid_prices[1]+ask_prices[1])/2)
                     / ((bid_prices[1]+ask_prices[1])/2) * 10000 <= 50,
                     arrayZip(ask_prices,ask_sizes)) AS near_asks
            FROM {db}.research_orderbook_ob200_snapshots
            WHERE symbol='BTCUSDT'
              AND event_time >= toDateTime64('2026-08-31 18:59:55',3,'UTC')
              AND event_time < toDateTime64('2026-08-31 19:00:05',3,'UTC')
        """,
        "joined_market": f"""
            SELECT m.bucket_time,m.trade_count,m.mid,o.bid_level_count,o.ask_level_count
            FROM {db}.research_market_1s m
            INNER JOIN {db}.research_orderbook_1s o
              ON m.symbol=o.symbol AND m.bucket_time=o.bucket_time
            WHERE m.symbol='BTCUSDT'
            ORDER BY m.bucket_time
        """,
    }
    return [_measure(client, name, sql) for name, sql in scenarios.items()]


def storage_stats(client: Any) -> list[dict[str, Any]]:
    result = rows(
        client,
        """
        SELECT table, sum(rows), sum(data_compressed_bytes),
               sum(data_uncompressed_bytes)
        FROM system.parts
        WHERE active AND database = %(database)s
          AND table IN ('research_orderbook_ob200_snapshots',
                        'research_orderbook_levels_pilot')
        GROUP BY table ORDER BY table
        """,
        {"database": TARGET_DATABASE},
    )
    return [
        {
            "table": str(table),
            "physical_rows": int(count),
            "compressed_bytes": int(compressed),
            "uncompressed_bytes": int(uncompressed),
        }
        for table, count, compressed, uncompressed in result
    ]
