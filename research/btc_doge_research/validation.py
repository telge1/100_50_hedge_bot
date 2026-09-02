"""Post-load physical/logical validation without FINAL."""

from __future__ import annotations

from typing import Any

from .clickhouse import rows
from .contracts import TARGET_DATABASE

FACT_KEYS = {
    "research_public_trades": "event_key",
    "research_liquidation_events": "event_key",
    "research_orderbook_ob200_snapshots": "event_key",
    "research_orderbook_1s": "bucket_key",
    "research_market_1s": "bucket_key",
    "research_market_1m": "bucket_key",
    "research_source_files": "source_file_id",
}


def table_identity(
    client: Any, table: str, *, symbol: str | None = None
) -> dict[str, Any]:
    key = FACT_KEYS[table]
    where = " WHERE symbol = %(symbol)s" if symbol else ""
    params = {"symbol": symbol} if symbol else {}
    row = rows(
        client,
        f"""
        SELECT count(), uniqExact({key}), count() - uniqExact({key}),
               minOrNull(toString({key})), maxOrNull(toString({key})),
               toString(sum(cityHash64(toString({key}))))
        FROM {TARGET_DATABASE}.{table}{where}
        """,
        params,
    )[0]
    return {
        "physical_rows": int(row[0]),
        "logical_keys": int(row[1]),
        "duplicate_keys": int(row[2]),
        "min_key": row[3],
        "max_key": row[4],
        "key_fingerprint": str(row[5]),
        "uses_final": False,
    }


def validate_ob_invariants(client: Any, symbol: str) -> dict[str, int]:
    row = rows(
        client,
        f"""
        SELECT
          countIf(length(bid_prices) != length(bid_sizes)),
          countIf(length(ask_prices) != length(ask_sizes)),
          countIf(length(bid_prices) > 200 OR length(ask_prices) > 200),
          countIf(arrayExists(x -> x <= 0, bid_prices)
                  OR arrayExists(x -> x <= 0, ask_prices)),
          countIf(arrayExists(x -> x < 0, bid_sizes)
                  OR arrayExists(x -> x < 0, ask_sizes)),
          countIf(bid_prices[1] >= ask_prices[1]),
          countIf(bid_prices != arrayReverse(arraySort(bid_prices))),
          countIf(ask_prices != arraySort(ask_prices)),
          countIf(positionCaseInsensitive(toString(bid_prices), 'nan') > 0
                  OR positionCaseInsensitive(toString(ask_prices), 'nan') > 0)
        FROM {TARGET_DATABASE}.research_orderbook_ob200_snapshots
        WHERE symbol = %(symbol)s
        """,
        {"symbol": symbol},
    )[0]
    names = (
        "pair_length_errors", "ask_pair_length_errors", "depth_errors",
        "nonpositive_price_errors", "negative_size_errors", "crossed_books",
        "bid_sort_errors", "ask_sort_errors", "nan_errors",
    )
    return {name: int(value) for name, value in zip(names, row)}


def target_tables(client: Any) -> list[str]:
    return [
        str(row[0])
        for row in rows(
            client,
            "SELECT name FROM system.tables WHERE database = %(database)s ORDER BY name",
            {"database": TARGET_DATABASE},
        )
    ]
