"""Source ↔ research public-trade parity audits."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .clickhouse import rows
from .trade_contract import BUILD_ID, ensure_utc_aware, iso_z, literal_utc


def classify_delta(delta_seconds: int | None) -> str:
    if delta_seconds is None:
        return "AMBIGUOUS"
    if delta_seconds == 0:
        return "EXACT_TIMESTAMP"
    if delta_seconds == -7200:
        return "SHIFTED_TIMESTAMP_CONSTANT"
    return "SHIFTED_TIMESTAMP_OTHER"


def shift_distribution(
    client: Any,
    *,
    research_table: str,
    symbol: str | None = None,
    use_final: bool = True,
) -> list[dict[str, Any]]:
    where = f"WHERE r.symbol = %(symbol)s" if symbol else ""
    params: dict[str, Any] = {"symbol": symbol} if symbol else {}
    final = "FINAL" if use_final else ""
    sql = f"""
    SELECT
      r.symbol,
      dateDiff('second', o.trade_ts, r.event_time) AS delta_seconds,
      count() AS row_count
    FROM {research_table} AS r {final}
    INNER JOIN (
      SELECT
        src.symbol AS symbol,
        src.trade_id AS trade_id,
        argMax(src.trade_ts, src.ingest_timestamp) AS trade_ts
      FROM orderbook_analysis.public_trades_canonical AS src
      GROUP BY src.symbol, src.trade_id
    ) AS o ON r.symbol = o.symbol AND r.trade_id = o.trade_id
    {where}
    GROUP BY r.symbol, delta_seconds
    ORDER BY r.symbol, delta_seconds
    """
    out = []
    for sym, delta, cnt in rows(client, sql, params):
        out.append(
            {
                "symbol": sym,
                "delta_seconds": int(delta),
                "row_count": int(cnt),
                "classification": classify_delta(int(delta)),
            }
        )
    return out


def parity_sample(
    client: Any,
    *,
    research_table: str,
    symbol: str,
    start: datetime,
    end: datetime,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Join OA source window to research by trade_id (research may be shifted)."""
    a, b = literal_utc(start), literal_utc(end)
    sql = f"""
    SELECT
      o.trade_id,
      o.trade_ts,
      r.event_time,
      dateDiff('second', o.trade_ts, r.event_time) AS delta_seconds,
      toFloat64(o.price), toFloat64(r.price),
      toFloat64(o.size), toFloat64(r.base_size),
      toString(o.side), toString(r.taker_side),
      toFloat64(o.notional), toFloat64(r.quote_notional)
    FROM (
      SELECT
        src.trade_id AS trade_id,
        argMax(src.trade_ts, src.ingest_timestamp) AS trade_ts,
        argMax(src.price, src.ingest_timestamp) AS price,
        argMax(src.size, src.ingest_timestamp) AS size,
        argMax(src.notional, src.ingest_timestamp) AS notional,
        argMax(src.side, src.ingest_timestamp) AS side
      FROM orderbook_analysis.public_trades_canonical AS src
      WHERE src.symbol = %(symbol)s
        AND src.trade_ts >= toDateTime64(%(a)s, 3, 'UTC')
        AND src.trade_ts < toDateTime64(%(b)s, 3, 'UTC')
      GROUP BY src.trade_id
    ) AS o
    LEFT JOIN {research_table} AS r
      ON r.symbol = %(symbol)s AND r.trade_id = o.trade_id
    ORDER BY o.trade_ts, o.trade_id
    LIMIT %(limit)s
    """
    out = []
    for row in rows(
        client,
        sql,
        {"symbol": symbol, "a": a, "b": b, "limit": limit},
    ):
        delta = None if row[3] is None else int(row[3])
        price_ok = row[5] is not None and abs(float(row[4]) - float(row[5])) < 1e-8
        size_ok = row[7] is not None and abs(float(row[6]) - float(row[7])) < 1e-9
        side_ok = row[9] is not None and str(row[8]) == str(row[9])
        notion_ok = row[11] is not None and abs(float(row[10]) - float(row[11])) < 1e-6
        if row[2] is None:
            cls = "SOURCE_ONLY"
        elif not (price_ok and size_ok and side_ok and notion_ok):
            cls = "FIELD_MISMATCH"
        else:
            cls = classify_delta(delta)
        out.append(
            {
                "symbol": symbol,
                "trade_id": str(row[0]),
                "source_trade_ts": iso_z(ensure_utc_aware(row[1])),
                "research_trade_ts": (
                    None if row[2] is None else iso_z(ensure_utc_aware(row[2]))
                ),
                "delta_seconds": delta,
                "price_match": price_ok,
                "size_match": size_ok,
                "side_match": side_ok,
                "quote_notional_match": notion_ok,
                "classification": cls,
            }
        )
    return out


def segment_parity(
    client: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    use_final: bool = True,
) -> dict[str, Any]:
    """Compare OA vs research_public_trades for one UTC segment after rematerialization."""
    a, b = literal_utc(start), literal_utc(end)
    final = "FINAL" if use_final else ""
    oa = rows(
        client,
        f"""
        SELECT count(), uniqExact(trade_id),
               min(trade_ts), max(trade_ts),
               min(trade_id), max(trade_id)
        FROM orderbook_analysis.public_trades_canonical {final}
        WHERE symbol = %(symbol)s
          AND trade_ts >= toDateTime64(%(a)s, 3, 'UTC')
          AND trade_ts < toDateTime64(%(b)s, 3, 'UTC')
        """,
        {"symbol": symbol, "a": a, "b": b},
    )[0]
    res = rows(
        client,
        f"""
        SELECT count(), uniqExact(trade_id),
               min(event_time), max(event_time)
        FROM btc_doge_research.research_public_trades {final}
        WHERE symbol = %(symbol)s
          AND event_time >= toDateTime64(%(a)s, 3, 'UTC')
          AND event_time < toDateTime64(%(b)s, 3, 'UTC')
          AND build_id = %(build_id)s
        """,
        {"symbol": symbol, "a": a, "b": b, "build_id": BUILD_ID},
    )[0]
    matched = rows(
        client,
        f"""
        SELECT
          count() AS joined,
          countIf(dateDiff('second', o.trade_ts, r.event_time) = 0) AS exact_ts,
          countIf(dateDiff('second', o.trade_ts, r.event_time) = -7200) AS shifted,
          countIf(
            abs(toFloat64(o.price) - toFloat64(r.price)) > 1e-8
            OR abs(toFloat64(o.size) - toFloat64(r.base_size)) > 1e-9
            OR toString(o.side) != toString(r.taker_side)
          ) AS field_mismatch
        FROM (
          SELECT
            src.trade_id AS trade_id,
            argMax(src.trade_ts, src.ingest_timestamp) AS trade_ts,
            argMax(src.price, src.ingest_timestamp) AS price,
            argMax(src.size, src.ingest_timestamp) AS size,
            argMax(src.side, src.ingest_timestamp) AS side
          FROM orderbook_analysis.public_trades_canonical AS src
          WHERE src.symbol = %(symbol)s
            AND src.trade_ts >= toDateTime64(%(a)s, 3, 'UTC')
            AND src.trade_ts < toDateTime64(%(b)s, 3, 'UTC')
          GROUP BY src.trade_id
        ) AS o
        INNER JOIN (
          SELECT trade_id, event_time, price, base_size, taker_side
          FROM btc_doge_research.research_public_trades {final}
          WHERE symbol = %(symbol)s AND build_id = %(build_id)s
        ) AS r ON o.trade_id = r.trade_id
        """,
        {"symbol": symbol, "a": a, "b": b, "build_id": BUILD_ID},
    )[0]
    source_count = int(oa[0])
    source_unique = int(oa[1])
    research_count = int(res[0])
    research_unique = int(res[1])
    exact = int(matched[1])
    shifted = int(matched[2])
    field_mm = int(matched[3])
    status = "PASS"
    if source_unique != research_unique or exact != source_unique or shifted or field_mm:
        status = "FAIL"
    return {
        "symbol": symbol,
        "segment_start": iso_z(start),
        "segment_end": iso_z(end),
        "source_row_count": source_count,
        "source_unique_trade_ids": source_unique,
        "research_row_count": research_count,
        "research_unique_trade_ids": research_unique,
        "exact_timestamp_matches": exact,
        "shifted_matches": shifted,
        "field_mismatches": field_mm,
        "source_min_ts": None if oa[2] is None else iso_z(ensure_utc_aware(oa[2])),
        "source_max_ts": None if oa[3] is None else iso_z(ensure_utc_aware(oa[3])),
        "research_min_ts": None if res[2] is None else iso_z(ensure_utc_aware(res[2])),
        "research_max_ts": None if res[3] is None else iso_z(ensure_utc_aware(res[3])),
        "status": status,
        "build_id": BUILD_ID,
    }
