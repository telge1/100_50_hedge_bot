"""7-day resumable public-trade backfill runner."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from signal_generator.bybit.public_trades.backfill_manifest import (
    BackfillManifestRow,
    BackfillManifestStore,
)
from signal_generator.bybit.public_trades.csv_parse import (
    PublicTradeParseError,
)
from signal_generator.bybit.public_trades.downloader import (
    DISK_FREE_MIN_BYTES,
    HttpxTransport,
    PublicTradeDownloadError,
    assert_disk_free,
    download_day_file,
    sha256_file,
)
from signal_generator.bybit.public_trades.importer import (
    FileParseStats,
    TradeIdUnreliableError,
    stream_parse_file,
)
from signal_generator.bybit.public_trades.urls import utc_day_bounds
from signal_generator.db.public_trades import CanonicalPublicTradeRepository

logger = logging.getLogger(__name__)

BYTES_PER_TRADE_PILOT = 177.2908546036995
MERGE_RESERVE = 1.5
MAX_SAFE_USE_BYTES = 430 * 1024 * 1024 * 1024


@dataclass
class SourceCheckResult:
    total: int
    ok: int
    missing: int
    total_content_length: int
    rows: list[dict[str, Any]]


def check_sources(
    manifest: BackfillManifestStore,
    *,
    transport: HttpxTransport | None = None,
    sleep_s: float = 0.05,
) -> SourceCheckResult:
    http = transport or HttpxTransport()
    ok = 0
    missing = 0
    total_cl = 0
    out_rows: list[dict[str, Any]] = []
    for row in manifest.as_rows():
        if row.status == "AUDITED":
            ok += 1
            if row.content_length:
                total_cl += int(row.content_length)
            out_rows.append(
                {
                    "symbol": row.symbol,
                    "utc_date": row.utc_date,
                    "url": row.url,
                    "http_status": row.http_status or 200,
                    "content_length": row.content_length,
                    "status": row.status,
                }
            )
            continue
        try:
            resp = http.head(row.url)
            cl = resp.headers.get("content-length")
            cl_int = int(cl) if cl and cl.isdigit() else None
            if resp.status_code == 200:
                manifest.set_status(
                    row,
                    "AVAILABLE",
                    http_status=200,
                    content_length=cl_int or 0,
                    error="",
                )
                ok += 1
                if cl_int:
                    total_cl += cl_int
            elif resp.status_code == 404:
                manifest.set_status(
                    row,
                    "ARCHIVE_UNAVAILABLE",
                    http_status=404,
                    error="HTTP 404",
                )
                missing += 1
            else:
                manifest.set_status(
                    row,
                    "FAILED",
                    http_status=resp.status_code,
                    error=f"HTTP {resp.status_code}",
                )
                missing += 1
            out_rows.append(
                {
                    "symbol": row.symbol,
                    "utc_date": row.utc_date,
                    "url": row.url,
                    "http_status": resp.status_code,
                    "content_length": cl_int,
                    "status": row.status,
                }
            )
        except Exception as exc:  # noqa: BLE001
            manifest.set_status(row, "FAILED", error=str(exc)[:300])
            missing += 1
            out_rows.append(
                {
                    "symbol": row.symbol,
                    "utc_date": row.utc_date,
                    "url": row.url,
                    "http_status": None,
                    "content_length": None,
                    "status": "FAILED",
                    "error": str(exc)[:300],
                }
            )
        time.sleep(sleep_s)
    return SourceCheckResult(
        total=len(manifest.as_rows()),
        ok=ok,
        missing=missing,
        total_content_length=total_cl,
        rows=out_rows,
    )


def project_7d_storage(
    *,
    total_gz_bytes: int,
    estimated_rows: int | None,
    free_bytes: int,
) -> dict[str, Any]:
    est_rows = estimated_rows
    if est_rows is None and total_gz_bytes > 0:
        est_rows = int(total_gz_bytes / 39.55301746409011)  # pilot gz bytes/trade
    ch_bytes = int((est_rows or 0) * BYTES_PER_TRADE_PILOT)
    with_merge = int(ch_bytes * MERGE_RESERVE)
    temp_peak = int(total_gz_bytes * 0.2)  # streaming cache upper bound estimate
    remaining = free_bytes - with_merge - temp_peak
    blocked = remaining < DISK_FREE_MIN_BYTES or with_merge > MAX_SAFE_USE_BYTES
    return {
        "total_gz_bytes": total_gz_bytes,
        "estimated_trade_rows": est_rows,
        "bytes_per_trade_pilot": BYTES_PER_TRADE_PILOT,
        "estimated_ch_bytes": ch_bytes,
        "merge_reserve_factor": MERGE_RESERVE,
        "estimated_ch_with_merge_bytes": with_merge,
        "estimated_temp_peak_bytes": temp_peak,
        "free_bytes_now": free_bytes,
        "hard_stop_free_bytes": DISK_FREE_MIN_BYTES,
        "max_safe_use_bytes": MAX_SAFE_USE_BYTES,
        "projected_remaining_free_bytes": remaining,
        "blocked": blocked,
    }


class BackfillRunner:
    def __init__(
        self,
        *,
        cache_dir: Path,
        manifest: BackfillManifestStore,
        repo: CanonicalPublicTradeRepository,
        transport: HttpxTransport | None = None,
        batch_size: int = 3000,
        pause_ms: int = 200,
    ) -> None:
        self.cache_dir = cache_dir
        self.manifest = manifest
        self.repo = repo
        self.transport = transport or HttpxTransport()
        self.batch_size = batch_size
        self.pause_ms = pause_ms

    def process_file(self, row: BackfillManifestRow) -> BackfillManifestRow:
        if row.status == "AUDITED":
            logger.info("skip AUDITED %s %s", row.symbol, row.utc_date)
            return row
        if row.status == "ARCHIVE_UNAVAILABLE":
            logger.info("skip ARCHIVE_UNAVAILABLE %s %s", row.symbol, row.utc_date)
            return row
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        assert_disk_free(self.cache_dir)
        day = date.fromisoformat(row.utc_date)
        symbol = row.symbol
        try:
            path = self._download(row, symbol, day)
        except PublicTradeDownloadError as exc:
            if exc.status == "SOURCE_FILE_MISSING":
                if row.status in {"PENDING", "AVAILABLE", "FAILED", "MISSING"}:
                    self.manifest.set_status(
                        row,
                        "ARCHIVE_UNAVAILABLE",
                        error=str(exc)[:400],
                        http_status=404,
                    )
                else:
                    row.status = "ARCHIVE_UNAVAILABLE"
                    row.error = str(exc)[:400]
                    self.manifest.save()
                return row
            raise
        digest = sha256_file(path)
        row.sha256 = digest
        self.manifest.save()
        day_start, day_end = utc_day_bounds(day)
        existing_day = self.repo.physical_and_logical_counts(
            symbols=(symbol,), start=day_start, end=day_end
        )
        skip_existing = int(existing_day["logical_unique_rows"] or 0) > 0

        inserted_total = 0
        skipped_total = 0
        ingest_ts = datetime.now(timezone.utc)
        batch: list = []
        last_stats: FileParseStats | None = None
        for trade, stats, dup_in_file in stream_parse_file(
            path, symbol=symbol, day=day, collect_ids=True
        ):
            last_stats = stats
            if trade is None or dup_in_file:
                continue
            batch.append(trade)
            if len(batch) >= self.batch_size:
                if skip_existing:
                    ins, sk = self.repo.insert_trades_skip_existing(
                        batch, ingest_timestamp=ingest_ts, source="archive"
                    )
                else:
                    ins, sk = self.repo.insert_trades(
                        batch, ingest_timestamp=ingest_ts, source="archive"
                    ), 0
                inserted_total += ins
                skipped_total += sk
                batch = []
                if self.pause_ms:
                    time.sleep(self.pause_ms / 1000.0)
        if batch:
            if skip_existing:
                ins, sk = self.repo.insert_trades_skip_existing(
                    batch, ingest_timestamp=ingest_ts, source="archive"
                )
            else:
                ins, sk = self.repo.insert_trades(
                    batch, ingest_timestamp=ingest_ts, source="archive"
                ), 0
            inserted_total += ins
            skipped_total += sk
        stats = last_stats or FileParseStats()
        self.manifest.set_status(
            row,
            "VERIFIED",
            source_rows=stats.rowcount_source,
            parsed_rows=stats.rowcount_parsed,
            invalid_rows=stats.invalid_rows,
            duplicate_trade_ids_in_file=stats.duplicate_rows,
            out_of_day_rows=stats.out_of_day_rows,
            min_event_time=stats.min_trade_ts.isoformat() if stats.min_trade_ts else "",
            max_event_time=stats.max_trade_ts.isoformat() if stats.max_trade_ts else "",
            compressed_bytes=path.stat().st_size,
            sha256=digest,
            error="",
        )
        if stats.empty_trade_id:
            self.manifest.set_status(row, "FAILED", error="empty trdMatchID")
            raise TradeIdUnreliableError(f"{symbol} {row.utc_date}")
        if stats.invalid_rows:
            self.manifest.set_status(
                row, "FAILED", error=f"invalid_rows={stats.invalid_rows}"
            )
            raise PublicTradeParseError(f"{symbol} {row.utc_date}: invalid rows")
        if stats.duplicate_rows:
            self.manifest.set_status(
                row, "FAILED", error=f"duplicate_in_file={stats.duplicate_rows}"
            )
            raise PublicTradeParseError(f"{symbol} {row.utc_date}: duplicate trade_ids")
        if stats.out_of_day_rows:
            self.manifest.set_status(
                row, "FAILED", error=f"out_of_day={stats.out_of_day_rows}"
            )
            raise PublicTradeParseError(f"{symbol} {row.utc_date}: out_of_day rows")

        now = datetime.now(timezone.utc).isoformat()
        self.manifest.set_status(
            row,
            "IMPORTED",
            inserted_rows=inserted_total,
            skipped_existing_rows=skipped_total,
            duplicate_trade_ids_against_target=skipped_total,
            import_finished_at=now,
            compressed_bytes=path.stat().st_size,
            error="",
        )
        return row

    def audit_file(self, row: BackfillManifestRow, *, cache_path: Path | None = None) -> BackfillManifestRow:
        if row.status == "AUDITED":
            return row
        if row.status != "IMPORTED":
            raise PublicTradeParseError(f"audit requires IMPORTED, got {row.status}")
        if row.invalid_rows != 0:
            raise PublicTradeParseError(f"audit fail invalid_rows={row.invalid_rows}")
        if row.duplicate_trade_ids_in_file != 0:
            raise PublicTradeParseError("audit fail dup in file")
        if row.out_of_day_rows != 0:
            raise PublicTradeParseError("audit fail out_of_day")
        if row.source_rows != row.parsed_rows + row.invalid_rows:
            raise PublicTradeParseError("audit fail source!=parsed+invalid")
        now = datetime.now(timezone.utc).isoformat()
        self.manifest.set_status(row, "AUDITED", audit_finished_at=now, error="")
        if cache_path is not None and cache_path.is_file():
            cache_path.unlink()
        return row

    def _download(self, row: BackfillManifestRow, symbol: str, day: date) -> Path:
        cached = self.cache_dir / f"{symbol}{day.isoformat()}.csv.gz"
        if cached.is_file() and cached.stat().st_size > 2:
            if row.status in {"PENDING", "AVAILABLE", "FAILED", "MISSING"}:
                self.manifest.set_status(
                    row,
                    "DOWNLOADED",
                    compressed_bytes=cached.stat().st_size,
                    http_status=200,
                    sha256=sha256_file(cached),
                    error="",
                )
            elif not row.sha256:
                row.sha256 = sha256_file(cached)
                self.manifest.save()
            return cached
        if not row.download_started_at:
            if row.status == "PENDING":
                self.manifest.set_status(
                    row,
                    "AVAILABLE",
                    download_started_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                row.download_started_at = datetime.now(timezone.utc).isoformat()
                self.manifest.save()
        path = download_day_file(
            symbol,
            day,
            self.cache_dir,
            transport=self.transport,
            disk_free_fn=assert_disk_free,
        )
        digest = sha256_file(path)
        if row.status in {"PENDING", "AVAILABLE", "FAILED", "MISSING"}:
            self.manifest.set_status(
                row,
                "DOWNLOADED",
                compressed_bytes=path.stat().st_size,
                http_status=200,
                sha256=digest,
                error="",
            )
        else:
            row.compressed_bytes = path.stat().st_size
            row.sha256 = digest
            self.manifest.save()
        return path

    def _verify(
        self, row: BackfillManifestRow, path: Path, symbol: str, day: date
    ) -> FileParseStats:
        last: FileParseStats | None = None
        for _trade, stats, _dup in stream_parse_file(path, symbol=symbol, day=day):
            last = stats
        stats = last or FileParseStats()
        self.manifest.set_status(
            row,
            "VERIFIED",
            source_rows=stats.rowcount_source,
            parsed_rows=stats.rowcount_parsed,
            invalid_rows=stats.invalid_rows,
            duplicate_trade_ids_in_file=stats.duplicate_rows,
            out_of_day_rows=stats.out_of_day_rows,
            min_event_time=stats.min_trade_ts.isoformat() if stats.min_trade_ts else "",
            max_event_time=stats.max_trade_ts.isoformat() if stats.max_trade_ts else "",
            error="",
        )
        return stats


def seed_pilot_audited(
    manifest: BackfillManifestStore,
    repo: CanonicalPublicTradeRepository,
    *,
    pilot_pairs: list[tuple[str, str]],
) -> None:
    """Mark pilot DOGE/LIT days AUDITED when CH already has expected uniq counts."""
    for symbol, utc_date in pilot_pairs:
        row = manifest.get(symbol, utc_date)
        if row is None:
            continue
        day = date.fromisoformat(utc_date)
        start, end = utc_day_bounds(day)
        counts = repo.physical_and_logical_counts(
            symbols=(symbol,), start=start, end=end
        )
        if counts["logical_unique_rows"] > 0 and row.status != "AUDITED":
            manifest.set_status(
                row,
                "AUDITED",
                skipped_existing_rows=counts["logical_unique_rows"],
                parsed_rows=counts["logical_unique_rows"],
                source_rows=counts["logical_unique_rows"],
                audit_finished_at=datetime.now(timezone.utc).isoformat(),
                error="seeded_from_pilot",
            )


def seed_full_canonical_days(
    manifest: BackfillManifestStore,
    coverage_rows: list[dict[str, Any]],
) -> int:
    """AUDITED only for archive-backed days whose event window covers the UTC day.

    Partial ClickHouse data (e.g. live-only morning hole) is never treated as complete.
    """
    from signal_generator.bybit.public_trades.window import classify_symbol_day

    seeded = 0
    by_key = {(str(r["symbol"]).upper(), str(r["utc_day"])): r for r in coverage_rows}
    for row in manifest.as_rows():
        if row.status == "AUDITED":
            continue
        meta = by_key.get((row.symbol.upper(), row.utc_date))
        if meta is None:
            continue
        klass = classify_symbol_day(
            logical_unique=int(meta.get("logical_unique") or 0),
            min_ts=meta.get("min_ts"),
            max_ts=meta.get("max_ts"),
            sources=list(meta.get("sources") or []),
            day=date.fromisoformat(row.utc_date),
        )
        if klass != "ALREADY_AUDITED":
            continue
        if row.status not in {"PENDING", "AVAILABLE"}:
            continue
        manifest.set_status(
            row,
            "AUDITED",
            parsed_rows=int(meta.get("logical_unique") or 0),
            source_rows=int(meta.get("logical_unique") or 0),
            skipped_existing_rows=int(meta.get("logical_unique") or 0),
            min_event_time=str(meta.get("min_ts") or ""),
            max_event_time=str(meta.get("max_ts") or ""),
            audit_finished_at=datetime.now(timezone.utc).isoformat(),
            error="seeded_from_full_canonical_day",
        )
        seeded += 1
    return seeded

