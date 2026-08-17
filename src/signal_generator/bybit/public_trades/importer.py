"""Idempotent streaming ingest of Bybit public-trade day files."""

from __future__ import annotations

import csv
import gzip
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from signal_generator.bybit.public_trades.csv_parse import (
    ParsedPublicTrade,
    PublicTradeParseError,
    parse_csv_trade_row,
)
from signal_generator.bybit.public_trades.downloader import (
    DISK_FREE_MIN_BYTES,
    HttpTransport,
    PublicTradeDownloadError,
    assert_disk_free,
    download_day_file,
)
from signal_generator.bybit.public_trades.manifest import ManifestRow, ManifestStore
from signal_generator.bybit.public_trades.urls import daily_url, parse_utc_day, utc_day_bounds
from signal_generator.db.public_trades import CanonicalPublicTradeRepository

logger = logging.getLogger(__name__)

OUT_OF_DAY_TOLERANCE = timedelta(seconds=1)


class AbortAfterRows(RuntimeError):
    """Controlled abort for resume tests (does not touch the collector)."""


class TradeIdUnreliableError(RuntimeError):
    """trade_id / trdMatchID cannot be used as a dedup key."""


@dataclass
class FileParseStats:
    header: list[str] = field(default_factory=list)
    rowcount_source: int = 0
    rowcount_parsed: int = 0
    invalid_rows: int = 0
    duplicate_rows: int = 0
    out_of_day_rows: int = 0
    unique_trade_ids: int = 0
    min_trade_ts: datetime | None = None
    max_trade_ts: datetime | None = None
    invalid_samples: list[str] = field(default_factory=list)
    empty_trade_id: int = 0
    trade_ids: set[str] = field(default_factory=set)


def _ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def stream_parse_file(
    path: Path,
    *,
    symbol: str,
    day: date,
    collect_ids: bool = True,
) -> Iterator[tuple[ParsedPublicTrade | None, FileParseStats, bool]]:
    """Yield (trade_or_none, stats_snapshot, is_duplicate).

    The same stats object is mutated; callers should copy fields they need.
    """
    stats = FileParseStats()
    start, end = utc_day_bounds(day)
    lo = start - OUT_OF_DAY_TOLERANCE
    hi = end + OUT_OF_DAY_TOLERANCE
    with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        stats.header = list(reader.fieldnames or [])
        for line_no, row in enumerate(reader, start=2):
            stats.rowcount_source += 1
            try:
                trade = parse_csv_trade_row(
                    row,
                    expected_symbol=symbol,
                    source_file=path.name,
                    source_line=line_no,
                )
            except PublicTradeParseError as exc:
                stats.invalid_rows += 1
                if "empty trdMatchID" in str(exc):
                    stats.empty_trade_id += 1
                if len(stats.invalid_samples) < 20:
                    stats.invalid_samples.append(str(exc))
                yield None, stats, False
                continue
            ts = _ensure_utc(trade.trade_ts)
            if ts < lo or ts >= hi:
                stats.out_of_day_rows += 1
            dup = trade.trade_id in stats.trade_ids
            if dup:
                stats.duplicate_rows += 1
            elif collect_ids:
                stats.trade_ids.add(trade.trade_id)
            stats.rowcount_parsed += 1
            stats.unique_trade_ids = len(stats.trade_ids) if collect_ids else stats.unique_trade_ids
            if stats.min_trade_ts is None or ts < stats.min_trade_ts:
                stats.min_trade_ts = ts
            if stats.max_trade_ts is None or ts > stats.max_trade_ts:
                stats.max_trade_ts = ts
            yield trade, stats, dup


class PublicTradeImporter:
    def __init__(
        self,
        *,
        cache_dir: Path,
        manifest: ManifestStore,
        repo: CanonicalPublicTradeRepository | None = None,
        transport: HttpTransport | None = None,
        batch_size: int = 3000,
        pause_ms: int = 200,
        source: str = "archive",
        disk_free_fn: Callable[[Path], int] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.manifest = manifest
        self.repo = repo
        self.transport = transport
        self.batch_size = max(1, batch_size)
        self.pause_ms = max(0, pause_ms)
        self.source = source
        self.disk_free_fn = disk_free_fn or (lambda p: assert_disk_free(p))

    def process_file(
        self,
        symbol: str,
        day: date,
        *,
        dry_run: bool = False,
        force_reimport: bool = False,
        abort_after_rows: int | None = None,
        skip_download: bool = False,
    ) -> ManifestRow:
        symbol = symbol.upper()
        day_s = day.isoformat()
        url = daily_url(symbol, day)
        row = self.manifest.ensure(symbol, day_s, url)

        if dry_run:
            return self._dry_run(row, symbol, day, skip_download=skip_download)

        if row.status in {"IMPORTED", "AUDITED"} and not force_reimport:
            logger.info("skip already %s %s %s", row.status, symbol, day_s)
            return row

        if self.repo is None:
            raise RuntimeError("repository required for import")

        self.disk_free_fn(self.cache_dir)
        path = self._ensure_downloaded(row, symbol, day, skip_download=skip_download)
        stats = self._verify(row, path, symbol, day)
        if stats.empty_trade_id or (stats.rowcount_parsed and stats.unique_trade_ids == 0):
            self.manifest.set_status(row, "FAILED", error="unreliable trade_id")
            raise TradeIdUnreliableError(f"{symbol} {day_s}: empty or missing trdMatchID")
        if stats.invalid_rows:
            self.manifest.set_status(
                row,
                "FAILED",
                error=f"invalid_rows={stats.invalid_rows}",
                invalid_rows=stats.invalid_rows,
            )
            raise PublicTradeParseError(f"{symbol} {day_s}: {stats.invalid_rows} invalid rows")

        inserted = 0
        batch: list[ParsedPublicTrade] = []
        ingest_ts = datetime.now(timezone.utc)
        try:
            for trade, _stats, dup in stream_parse_file(path, symbol=symbol, day=day, collect_ids=False):
                if trade is None or dup:
                    continue
                batch.append(trade)
                if len(batch) >= self.batch_size:
                    inserted += self.repo.insert_trades(
                        batch, ingest_timestamp=ingest_ts, source=self.source
                    )
                    batch = []
                    if self.pause_ms:
                        time.sleep(self.pause_ms / 1000.0)
                    if abort_after_rows is not None and inserted >= abort_after_rows:
                        raise AbortAfterRows(
                            f"controlled abort after {inserted} rows for {symbol} {day_s}"
                        )
            if batch:
                inserted += self.repo.insert_trades(
                    batch, ingest_timestamp=ingest_ts, source=self.source
                )
        except AbortAfterRows as exc:
            self.manifest.set_status(
                row,
                "FAILED",
                error=str(exc),
                inserted_rows=inserted,
                local_path=str(path),
            )
            raise
        except Exception as exc:
            self.manifest.set_status(row, "FAILED", error=str(exc), inserted_rows=inserted)
            raise

        return self.manifest.set_status(
            row,
            "IMPORTED",
            inserted_rows=inserted,
            local_path=str(path),
            compressed_bytes=path.stat().st_size,
            error="",
        )

    def _dry_run(
        self,
        row: ManifestRow,
        symbol: str,
        day: date,
        *,
        skip_download: bool,
    ) -> ManifestRow:
        path = self._ensure_downloaded(row, symbol, day, skip_download=skip_download)
        stats = self._verify(row, path, symbol, day)
        if stats.empty_trade_id:
            self.manifest.set_status(row, "FAILED", error="unreliable trade_id: empty trdMatchID")
            raise TradeIdUnreliableError(f"{symbol} {day}: empty trdMatchID")
        return row

    def _ensure_downloaded(
        self,
        row: ManifestRow,
        symbol: str,
        day: date,
        *,
        skip_download: bool,
    ) -> Path:
        if row.local_path:
            path = Path(row.local_path)
            if path.is_file():
                if row.status == "PENDING":
                    self.manifest.set_status(
                        row,
                        "DOWNLOADED",
                        local_path=str(path),
                        compressed_bytes=path.stat().st_size,
                    )
                return path
        if skip_download:
            raise PublicTradeDownloadError("FAILED", f"missing local file for {symbol} {day}")
        path = download_day_file(
            symbol,
            day,
            self.cache_dir,
            transport=self.transport,
            disk_free_fn=self.disk_free_fn,
        )
        self.manifest.set_status(
            row,
            "DOWNLOADED",
            local_path=str(path),
            compressed_bytes=path.stat().st_size,
            http_status=200,
            error="",
        )
        return path

    def _verify(self, row: ManifestRow, path: Path, symbol: str, day: date) -> FileParseStats:
        last: FileParseStats | None = None
        for _trade, stats, _dup in stream_parse_file(path, symbol=symbol, day=day):
            last = stats
        stats = last or FileParseStats()
        self.manifest.set_status(
            row,
            "VERIFIED",
            rowcount_source=stats.rowcount_source,
            rowcount_parsed=stats.rowcount_parsed,
            invalid_rows=stats.invalid_rows,
            duplicate_rows=stats.duplicate_rows,
            out_of_day_rows=stats.out_of_day_rows,
            unique_trade_ids=stats.unique_trade_ids,
            min_trade_ts=stats.min_trade_ts.isoformat() if stats.min_trade_ts else "",
            max_trade_ts=stats.max_trade_ts.isoformat() if stats.max_trade_ts else "",
            compressed_bytes=path.stat().st_size,
            local_path=str(path),
            error="",
        )
        return stats
