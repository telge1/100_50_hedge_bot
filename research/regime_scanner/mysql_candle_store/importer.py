"""General feather importer into the candle store (direct TFs including HTF)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from research.regime_scanner.mysql_candle_store.candle_timeframes import (
    candle_close_time,
    is_importable_timeframe,
    normalize_timeframe,
)
from research.regime_scanner.mysql_candle_store.hashing import sha256_file
from research.regime_scanner.mysql_candle_store.schema import (
    DIRECT_IMPORT_TIMEFRAMES,
    SOURCE_FREQTRADE_DIRECT,
)
from research.regime_scanner.mysql_candle_store.store_memory import CandleStore, UpsertStats
from research.regime_scanner.mysql_candle_store.validation import ValidationReport, validate_ohlcv_frame
from research.regime_scanner.timeframes import ensure_utc_timestamp


@dataclass
class ImportReport:
    input_path: str
    input_sha256: str
    exchange: str
    symbol: str
    timeframe: str
    dry_run: bool = False
    source: str = SOURCE_FREQTRADE_DIRECT
    source_timeframe: str | None = None
    rows_read: int = 0
    rows_valid: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    skipped_protected: int = 0
    conflicts: int = 0
    conflict_details: list[dict[str, Any]] = field(default_factory=list)
    duplicates: int = 0
    gaps: int = 0
    misaligned_opens: int = 0
    start: str | None = None
    end: str | None = None
    errors: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _frame_to_rows(
    frame: pd.DataFrame,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    source: str,
    source_hash: str,
    source_timeframe: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        open_time = ensure_utc_timestamp(row["date"])
        close_time = candle_close_time(open_time, timeframe)
        rows.append(
            {
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "open_time": open_time,
                "close_time": close_time,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "is_closed": True,
                "source": source,
                "source_timeframe": source_timeframe,
                "source_hash": source_hash,
            }
        )
    return rows


def import_feather(
    store: CandleStore,
    *,
    input_path: str | Path,
    exchange: str,
    symbol: str,
    timeframe: str,
    dry_run: bool = False,
    batch_size: int = 2000,
    record_validation_metadata: bool = True,
) -> ImportReport:
    """Import a Freqtrade OHLCV feather as ``freqtrade_direct`` candles.

    Supports direct-import TFs including HTF ``4h`` / ``1d`` / ``1w`` / ``1M``.
    Does not modify the input file. Only closed candles are persisted.
    """
    tf = normalize_timeframe(timeframe)
    if not is_importable_timeframe(tf):
        raise ValueError(f"unsupported timeframe: {timeframe!r}; allowed={DIRECT_IMPORT_TIMEFRAMES}")

    path = Path(input_path)
    if not path.is_file():
        raise FileNotFoundError(f"feather not found: {path}")

    digest = sha256_file(path)
    raw = pd.read_feather(path)
    frame, validation = validate_ohlcv_frame(raw, timeframe=tf)
    report = ImportReport(
        input_path=str(path.resolve()),
        input_sha256=digest,
        exchange=exchange,
        symbol=symbol,
        timeframe=tf,
        dry_run=dry_run,
        source=SOURCE_FREQTRADE_DIRECT,
        source_timeframe=tf,
        rows_read=validation.rows_read,
        duplicates=validation.duplicate_timestamps,
        gaps=validation.gap_count,
        misaligned_opens=validation.misaligned_opens,
        start=validation.start,
        end=validation.end,
        errors=list(validation.errors),
        validation=_validation_dict(validation),
    )
    if not validation.ok:
        report.skipped = report.rows_read
        return report

    report.rows_valid = validation.rows_valid
    rows = _frame_to_rows(
        frame,
        exchange=exchange,
        symbol=symbol,
        timeframe=tf,
        source=SOURCE_FREQTRADE_DIRECT,
        source_hash=digest,
        source_timeframe=tf,
    )
    if dry_run:
        report.skipped = len(rows)
        if record_validation_metadata:
            # Dry-run does not persist; metadata only after real writes unless store is memory ephemeral.
            pass
        return report

    stats = UpsertStats()
    for i in range(0, len(rows), max(1, int(batch_size))):
        stats.merge(store.upsert_candles(rows[i : i + batch_size]))
    report.inserted = stats.inserted
    report.updated = stats.updated
    report.unchanged = stats.unchanged
    report.skipped_protected = stats.skipped_protected
    report.conflicts = stats.conflicts
    report.conflict_details = list(stats.conflict_details[:50])
    if stats.conflicts:
        report.errors.append(f"source conflicts during import: {stats.conflicts}")

    if record_validation_metadata and not report.errors:
        store.insert_validation_run(
            {
                "validation_type": "feather_direct_import",
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": tf,
                "canonical_source": "freqtrade_direct",
                "comparison_source": None,
                "input_path": str(path.resolve()),
                "input_sha256": digest,
                "common_start": report.start,
                "common_end": report.end,
                "row_count": report.rows_valid,
                "deterministic_output_hash": digest,
                "metadata_json": {
                    "source": SOURCE_FREQTRADE_DIRECT,
                    "source_timeframe": tf,
                    "inserted": report.inserted,
                    "updated": report.updated,
                    "unchanged": report.unchanged,
                    "gaps": report.gaps,
                },
            }
        )
    return report


def import_5m_feather(
    store: CandleStore,
    *,
    input_path: str | Path,
    exchange: str,
    symbol: str,
    dry_run: bool = False,
    batch_size: int = 2000,
) -> ImportReport:
    """Backward-compatible wrapper around :func:`import_feather` for 5m."""
    return import_feather(
        store,
        input_path=input_path,
        exchange=exchange,
        symbol=symbol,
        timeframe="5m",
        dry_run=dry_run,
        batch_size=batch_size,
    )


def _validation_dict(validation: ValidationReport) -> dict[str, Any]:
    return {
        "rows_read": validation.rows_read,
        "rows_valid": validation.rows_valid,
        "duplicate_timestamps": validation.duplicate_timestamps,
        "gap_count": validation.gap_count,
        "gap_samples": validation.gap_samples,
        "ohlc_violations": validation.ohlc_violations,
        "ohlc_violation_samples": validation.ohlc_violation_samples,
        "negative_volume_count": validation.negative_volume_count,
        "null_count": validation.null_count,
        "misaligned_opens": validation.misaligned_opens,
        "misaligned_samples": validation.misaligned_samples,
        "sorted": validation.sorted,
        "errors": list(validation.errors),
    }
