"""Throttle historical public-trade files into public_trades_archive.

Safety:
- Writes only ``orderbook_analysis.public_trades_archive``.
- Never INSERT/ALTER live ``public_trades`` or ``signal_generator.*``.
- Small batches + sleep so the candle collector on the same ClickHouse
  instance is not starved.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from orderbook_analyse.public_trade_source.csv_gzip_source import FILENAME_RE
from orderbook_analyse.public_trade_source.csv_parse import (
    PublicTradeParseError,
    parse_csv_trade_row,
)

logger = logging.getLogger(__name__)

ARCHIVE_DATABASE = "orderbook_analysis"
ARCHIVE_TABLE = "public_trades_archive"
ARCHIVE_FQN = f"{ARCHIVE_DATABASE}.{ARCHIVE_TABLE}"

FORBIDDEN_DATABASES = frozenset({"signal_generator"})
FORBIDDEN_TABLES = frozenset(
    {
        "public_trades",
        "orderbook_deltas",
        "ticker_samples",
        "liquidations",
        "recorder_health",
        "candles_1m",
        "signals",
        "signal_processing_state",
    }
)

ARCHIVE_COLUMNS = [
    "trade_ts",
    "received_ts",
    "symbol",
    "trade_id",
    "side",
    "price",
    "quantity",
    "notional",
    "tick_direction",
    "is_block_trade",
    "is_rpi_trade",
    "ingest_source",
    "source_file",
]

CREATE_ARCHIVE_SQL = """
CREATE TABLE IF NOT EXISTS orderbook_analysis.public_trades_archive
(
    `trade_ts` DateTime64(3, 'UTC'),
    `received_ts` DateTime64(6, 'UTC'),
    `symbol` LowCardinality(String),
    `trade_id` String,
    `side` Enum8('Buy' = 1, 'Sell' = 2),
    `price` Decimal(18, 8),
    `quantity` Decimal(18, 8),
    `notional` Decimal(18, 8),
    `tick_direction` LowCardinality(String),
    `is_block_trade` UInt8,
    `is_rpi_trade` UInt8,
    `ingest_source` LowCardinality(String),
    `source_file` String
)
ENGINE = ReplacingMergeTree(received_ts)
PARTITION BY toYYYYMMDD(trade_ts)
ORDER BY (symbol, trade_ts, trade_id)
SETTINGS index_granularity = 8192
"""

INSERT_SETTINGS = {
    "priority": 16,
    "max_insert_threads": 1,
    "max_threads": 1,
    "max_block_size": 8192,
}

_SQL_TABLE_RE = re.compile(
    r"(?:FROM|INTO|TABLE|UPDATE|JOIN)\s+(?:IF\s+(?:NOT\s+)?EXISTS\s+)?(?:`?(\w+)`?\.)?`?(\w+)`?",
    re.IGNORECASE,
)


class ArchiveIngestError(RuntimeError):
    """Refused or failed archive ingest."""


def assert_archive_table(table: str) -> str:
    raw = str(table).strip().strip("`")
    db = ""
    name = raw
    if "." in raw:
        db, name = raw.split(".", 1)
    name = name.strip("`")
    db = db.strip("`")
    if name in FORBIDDEN_TABLES:
        raise ArchiveIngestError(f"refusing write to live/scanner table: {raw}")
    if db in FORBIDDEN_DATABASES:
        raise ArchiveIngestError(f"refusing write to scanner database: {raw}")
    if name != ARCHIVE_TABLE:
        raise ArchiveIngestError(f"archive ingest only allows {ARCHIVE_TABLE}; got {raw}")
    if db and db != ARCHIVE_DATABASE:
        raise ArchiveIngestError(f"archive ingest only allows {ARCHIVE_DATABASE}; got {raw}")
    return ARCHIVE_FQN


def assert_archive_sql(sql: str) -> None:
    upper = f" {sql.lstrip().upper()} "
    for token in (" DROP ", " DELETE ", " TRUNCATE ", " ALTER ", " RENAME ", " OPTIMIZE "):
        if token in upper:
            raise ArchiveIngestError(f"forbidden SQL token: {token.strip()}")
    if "SIGNAL_GENERATOR" in upper:
        raise ArchiveIngestError("refusing SQL that mentions signal_generator")
    if "CANDLES_1M" in upper:
        raise ArchiveIngestError("refusing SQL that mentions candles_1m")
    for m in _SQL_TABLE_RE.finditer(sql):
        db, tbl = m.group(1) or "", m.group(2)
        if tbl and tbl.lower() in {"if", "not", "exists"}:
            continue
        if db:
            assert_archive_table(f"{db}.{tbl}")
        elif tbl:
            assert_archive_table(tbl)


def parse_rpi_flag(row: dict[str, str]) -> int:
    raw = str(row.get("RPI") or "0").strip().lower()
    return 1 if raw in {"1", "true", "yes"} else 0


def trade_to_archive_row(
    trade,
    *,
    received_ts: datetime,
    ingest_source: str,
    is_rpi_trade: int = 0,
) -> tuple:
    return (
        trade.trade_ts,
        received_ts,
        trade.symbol,
        trade.trade_id,
        trade.side,
        trade.price,
        trade.size,
        trade.notional,
        trade.tick_direction,
        0,
        int(is_rpi_trade),
        ingest_source,
        trade.source_file,
    )


def discover_trade_files(roots: list[Path], symbol: str | None = None) -> list[Path]:
    """Find ``SYMBOLYYYY-MM-DD.csv.gz`` files, nested or flat. Prefer gz over csv."""
    found: dict[tuple[str, str], Path] = {}
    want = symbol.upper() if symbol else None
    for root in roots:
        root = Path(root)
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.csv.gz")):
            m = FILENAME_RE.match(path.name)
            if m is None:
                continue
            sym = m.group("symbol").upper()
            if want and sym != want:
                continue
            found[(sym, m.group("date"))] = path
    return [found[k] for k in sorted(found)]


def iter_file_trades(path: Path, *, symbol: str) -> Iterator[tuple[Any, int]]:
    with gzip.open(path, "rt", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ArchiveIngestError(f"empty CSV header in {path}")
        for line_no, row in enumerate(reader, start=2):
            trade = parse_csv_trade_row(
                row,
                expected_symbol=symbol,
                source="archive_ingest",
                source_file=str(path),
                source_line=line_no,
            )
            yield trade, parse_rpi_flag(row)


@dataclass
class FileIngestResult:
    path: str
    symbol: str
    status: str
    rows_read: int = 0
    rows_inserted: int = 0
    invalid_rows: int = 0
    batches: int = 0
    error: str | None = None


@dataclass
class IngestRunResult:
    table: str = ARCHIVE_FQN
    dry_run: bool = False
    files: list[FileIngestResult] = field(default_factory=list)
    rows_inserted: int = 0
    paused_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ArchiveIngestWriter:
    """INSERT-only client locked to public_trades_archive."""

    def __init__(self, client: Any, *, database: str):
        if database != ARCHIVE_DATABASE:
            raise ArchiveIngestError(
                f"refusing client database={database!r}; expected {ARCHIVE_DATABASE}"
            )
        self._client = client
        self.database = database

    def ensure_table(self) -> None:
        assert_archive_sql(CREATE_ARCHIVE_SQL)
        self._client.command(CREATE_ARCHIVE_SQL)

    def insert_rows(self, rows: list[tuple]) -> None:
        if not rows:
            return
        assert_archive_table(ARCHIVE_FQN)
        self._client.insert(
            ARCHIVE_FQN,
            rows,
            column_names=ARCHIVE_COLUMNS,
            settings=INSERT_SETTINGS,
        )


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"completed_files": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path, completed: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"completed_files": completed, "updated_at": datetime.now(timezone.utc).isoformat()}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def ingest_files(
    *,
    writer: ArchiveIngestWriter | None,
    files: list[Path],
    batch_size: int = 4000,
    pause_ms: int = 250,
    dry_run: bool = False,
    ingest_source: str = "bybit_public_csv_gz",
    checkpoint_path: Path | None = None,
    skip_invalid: bool = False,
) -> IngestRunResult:
    if batch_size < 1:
        raise ArchiveIngestError("batch_size must be >= 1")
    if pause_ms < 0:
        raise ArchiveIngestError("pause_ms must be >= 0")
    if not dry_run and writer is None:
        raise ArchiveIngestError("writer required unless dry_run")

    completed: list[str] = []
    if checkpoint_path is not None:
        completed = list(load_checkpoint(checkpoint_path).get("completed_files") or [])
    done = set(completed)

    run = IngestRunResult(dry_run=dry_run, paused_ms=pause_ms)
    received_ts = datetime.now(timezone.utc)

    for path in files:
        key = str(path.resolve())
        m = FILENAME_RE.match(path.name)
        if m is None:
            run.files.append(
                FileIngestResult(path=key, symbol="", status="SKIPPED_NAME", error="filename")
            )
            continue
        symbol = m.group("symbol").upper()
        if key in done:
            run.files.append(FileIngestResult(path=key, symbol=symbol, status="SKIPPED_CHECKPOINT"))
            continue

        result = FileIngestResult(path=key, symbol=symbol, status="PENDING")
        batch: list[tuple] = []
        try:
            for trade, is_rpi in iter_file_trades(path, symbol=symbol):
                result.rows_read += 1
                batch.append(
                    trade_to_archive_row(
                        trade,
                        received_ts=received_ts,
                        ingest_source=ingest_source,
                        is_rpi_trade=is_rpi,
                    )
                )
                if len(batch) >= batch_size:
                    if not dry_run:
                        writer.insert_rows(batch)  # type: ignore[union-attr]
                    result.rows_inserted += len(batch)
                    result.batches += 1
                    batch = []
                    if pause_ms:
                        time.sleep(pause_ms / 1000.0)
            if batch:
                if not dry_run:
                    writer.insert_rows(batch)  # type: ignore[union-attr]
                result.rows_inserted += len(batch)
                result.batches += 1
                batch = []
                if pause_ms:
                    time.sleep(pause_ms / 1000.0)
            result.status = "DRY_RUN" if dry_run else "INSERTED"
            completed.append(key)
            if checkpoint_path is not None and not dry_run:
                save_checkpoint(checkpoint_path, completed)
        except PublicTradeParseError as exc:
            result.invalid_rows += 1
            result.error = str(exc)
            result.status = "INVALID_SKIPPED" if skip_invalid else "FAILED"
            if not skip_invalid:
                run.files.append(result)
                run.rows_inserted += result.rows_inserted
                raise
        except Exception as exc:
            result.error = str(exc)
            result.status = "FAILED"
            run.files.append(result)
            run.rows_inserted += result.rows_inserted
            raise
        run.files.append(result)
        run.rows_inserted += result.rows_inserted
        logger.info(
            "archive ingest %s %s rows=%s batches=%s",
            result.status,
            path.name,
            result.rows_inserted,
            result.batches,
        )
    return run
