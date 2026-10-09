"""Read the missing DOGE tail with the Phase 11R selection and publish a new file."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ..immutable_candle_snapshot_v1 import SOURCE_QUERY_SEMANTICS, CandleInputRow, publish_snapshot

TAIL_START = datetime(2026, 9, 30, 12, 1, tzinfo=timezone.utc)
TAIL_END_EXCLUSIVE = datetime(2026, 9, 30, 23, 55, tzinfo=timezone.utc)
EXPECTED_ROWS = 714

# Same predicate as run_phase11r_full_rolling_input_poc._fetch and the Phase 11R manifest.
TAIL_QUERY = """
SELECT exchange, symbol, interval, open_time, close_time, open, high, low, close,
       volume, turnover, is_closed, source, source_event_time, ingested_at
FROM {database}.candles_1m FINAL
WHERE exchange = {{ex:String}} AND symbol = 'DOGEUSDT' AND interval = '1m'
  AND is_closed = 1
  AND open_time >= {{a:DateTime64(3, 'UTC')}}
  AND open_time < {{b:DateTime64(3, 'UTC')}}
ORDER BY open_time
"""


def _utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_tail(client, database: str, exchange: str = "bybit") -> list[CandleInputRow]:
    rows = []
    query = TAIL_QUERY.format(database=database)
    for record in client.query(
        query,
        parameters={"ex": exchange, "a": TAIL_START, "b": TAIL_END_EXCLUSIVE},
    ).result_rows:
        rows.append(
            CandleInputRow(
                exchange=str(record[0]),
                symbol=str(record[1]),
                interval=str(record[2]),
                open_time=_utc(record[3]),
                close_time=_utc(record[4]),
                open=Decimal(record[5]),
                high=Decimal(record[6]),
                low=Decimal(record[7]),
                close=Decimal(record[8]),
                volume=Decimal(record[9]),
                turnover=Decimal(record[10]),
                is_closed=int(record[11]),
                source=str(record[12]),
                source_event_time=_utc(record[13]) if record[13] is not None else None,
                ingested_at=_utc(record[14]),
            )
        )
    return rows


def publish_tail(rows: list[CandleInputRow], directory: Path, *, base_directory: Path) -> dict:
    if directory.resolve() == base_directory.resolve() or base_directory.resolve() in directory.resolve().parents:
        raise RuntimeError("refusing to write into the Phase 11R snapshot directory")
    if len(rows) != EXPECTED_ROWS:
        raise RuntimeError(f"refusing to publish {len(rows)} rows; expected {EXPECTED_ROWS}")
    first = min(row.open_time for row in rows)
    last = max(row.open_time for row in rows)
    if int(first.timestamp()) != int(TAIL_START.timestamp()):
        raise RuntimeError("tail does not start at 12:01")
    if int(last.timestamp()) != int(TAIL_END_EXCLUSIVE.timestamp()) - 60:
        raise RuntimeError("tail does not end at 23:54")
    manifest = publish_snapshot(
        rows,
        directory,
        range_start=TAIL_START,
        range_end_exclusive=TAIL_END_EXCLUSIVE,
        source_table="signal_generator.candles_1m",
    )
    manifest["source_query_semantics"] = SOURCE_QUERY_SEMANTICS
    return manifest
