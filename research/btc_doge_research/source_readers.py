"""Bounded, non-FINAL readers for canonical source tables."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from .clickhouse import rows


def _params(symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    return {"symbol": symbol, "start": start, "end": end}


def read_public_trades(
    client: Any, symbol: str, start: datetime, end: datetime
) -> tuple[list[tuple], dict[str, Any]]:
    params = _params(symbol, start, end)
    stats = rows(
        client,
        """
        SELECT count(), uniqExact(trade_id), count() - uniqExact(trade_id),
               countIf(trade_id IN (
                   SELECT trade_id
                   FROM orderbook_analysis.public_trades_canonical
                   WHERE symbol = %(symbol)s
                     AND trade_ts >= %(start)s AND trade_ts < %(end)s
                   GROUP BY trade_id HAVING count() > 1
               ))
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol = %(symbol)s
          AND trade_ts >= %(start)s AND trade_ts < %(end)s
        """,
        params,
    )[0]
    logical = rows(
        client,
        """
        SELECT
            trade_id,
            argMax(trade_ts, ingest_timestamp),
            max(ingest_timestamp),
            argMax(price, ingest_timestamp),
            argMax(size, ingest_timestamp),
            argMax(notional, ingest_timestamp),
            argMax(side, ingest_timestamp),
            argMax(source, ingest_timestamp)
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol = %(symbol)s
          AND trade_ts >= %(start)s AND trade_ts < %(end)s
        GROUP BY trade_id
        ORDER BY argMax(trade_ts, ingest_timestamp), trade_id
        """,
        params,
    )
    return logical, {
        "physical_rows": int(stats[0]),
        "logical_rows": int(stats[1]),
        "physical_duplicates": int(stats[2]),
        "rows_in_duplicate_groups": int(stats[3]),
        "uses_final": False,
    }


def read_liquidations(
    client: Any, symbol: str, start: datetime, end: datetime
) -> tuple[list[tuple], dict[str, Any]]:
    params = _params(symbol, start, end)
    stats = rows(
        client,
        """
        SELECT count(), uniqExact(event_key), count() - uniqExact(event_key)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol = %(symbol)s
          AND event_time >= %(start)s AND event_time < %(end)s
        """,
        params,
    )[0]
    logical = rows(
        client,
        """
        SELECT
            event_key,
            argMax(event_time, inserted_at),
            argMax(received_at, inserted_at),
            argMax(position_side_raw, inserted_at),
            argMax(liquidated_position_side, inserted_at),
            argMax(size, inserted_at),
            argMax(bankruptcy_price, inserted_at),
            max(inserted_at)
        FROM orderbook_analysis.all_liquidations
        WHERE symbol = %(symbol)s
          AND event_time >= %(start)s AND event_time < %(end)s
        GROUP BY event_key
        ORDER BY argMax(event_time, inserted_at), event_key
        """,
        params,
    )
    return logical, {
        "physical_rows": int(stats[0]),
        "logical_rows": int(stats[1]),
        "physical_duplicates": int(stats[2]),
        "uses_final": False,
    }


def read_open_interest(
    client: Any, symbol: str, start: datetime, end: datetime
) -> list[tuple]:
    return rows(
        client,
        """
        SELECT
            bucket_time,
            argMax(open_interest, inserted_at),
            argMax(open_interest_value, inserted_at),
            argMax(state_age_ms, inserted_at),
            argMax(state_valid, inserted_at),
            argMax(source_event_time, inserted_at),
            max(inserted_at)
        FROM orderbook_analysis.open_interest_5s
        WHERE symbol = %(symbol)s
          AND bucket_time >= %(start)s AND bucket_time < %(end)s
        GROUP BY bucket_time
        ORDER BY bucket_time
        """,
        _params(symbol, start, end),
    )


def decimal_sum(values: list[Any]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal("0"))
