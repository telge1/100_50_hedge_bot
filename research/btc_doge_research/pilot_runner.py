"""Bounded Phase-1 pilot orchestration."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from .batch_state import BATCH_COLUMNS, batch_row, input_fingerprint
from .clickhouse import (
    ensure_batch_available,
    ensure_source_file_available,
    execute_ddl,
    insert,
    rows,
)
from .config import OB200_ROOT, PilotWindow
from .contracts import (
    OB200_CONTRACT_VERSION,
    PROCESSOR_VERSION,
    RESEARCH_CONTRACT_VERSION,
    TARGET_DATABASE,
    sanitize_json,
)
from .coverage import COVERAGE_COLUMNS, coverage_row
from .ddl import statements
from .liquidation_transform import (
    LIQUIDATION_COLUMNS,
    transform_liquidations,
)
from .market_aggregation import (
    MARKET_MINUTE_COLUMNS,
    MARKET_SECOND_COLUMNS,
    build_market_minutes,
    build_market_seconds,
)
from .ob200_parser import FullBookEvent, OB200SegmentReader
from .ob200_storage import (
    ORDERBOOK_SECOND_COLUMNS,
    SNAPSHOT_COLUMNS,
    build_orderbook_seconds,
    snapshot_row,
)
from .source_file_registry import SourceFile, load_source_file
from .source_readers import (
    read_liquidations,
    read_open_interest,
    read_public_trades,
)
from .trade_transform import TRADE_COLUMNS, transform_trades

SOURCE_FILE_COLUMNS = (
    "source_file_id", "source_relative_path", "file_name", "symbol",
    "segment_start", "segment_end", "file_size", "compression",
    "source_fingerprint", "parser_version", "source_contract_version",
    "event_count", "import_status", "import_batch_id", "first_event_time",
    "last_event_time", "duplicate_status", "overlap_status", "error_status",
    "registered_at",
)

LEVEL_COLUMNS = (
    "symbol", "event_key", "event_time", "side", "level_rank", "price",
    "size", "pilot_status", "ingestion_batch_id",
)

PIPELINE_COLUMNS = (
    "state_key", "processor", "source_id", "symbol", "last_read_ts",
    "last_finalized_ts", "watermark_ts", "overlap_seconds",
    "last_successful_run", "rows_read", "rows_written", "processor_version",
    "contract_version", "status", "error",
)


def create_schema(client: Any) -> list[str]:
    executed: list[str] = []
    for statement in statements():
        execute_ddl(client, statement)
        executed.append(statement + ";")
    return executed


def discover_sources(window: PilotWindow) -> list[SourceFile]:
    hour = window.start.replace(minute=0, second=0, microsecond=0)
    found: list[SourceFile] = []
    while hour < window.end:
        next_hour = hour + timedelta(hours=1)
        name = (
            f"{window.symbol}_{hour:%Y%m%dT%H%M%SZ}_"
            f"{next_hour:%Y%m%dT%H%M%SZ}_ob200_v3.zst"
        )
        path = (
            OB200_ROOT
            / window.symbol
            / f"{hour.year:04d}"
            / f"{hour.month:02d}"
            / f"{hour.day:02d}"
            / name
        )
        found.append(load_source_file(path, OB200_ROOT))
        hour = next_hour
    return found


def validate_sources(
    sources: list[SourceFile], window: PilotWindow
) -> list[dict[str, Any]]:
    audits: list[dict[str, Any]] = []
    for source in sources:
        reader = OB200SegmentReader(source, window.symbol)
        for _ in reader.iter_full_books(window.start, window.end):
            pass
        audit = asdict(reader.audit)
        if not reader.audit.full_file_consumed:
            raise RuntimeError(f"source file was not fully consumed: {source.path}")
        if not reader.audit.effective_replayable:
            raise RuntimeError(f"source is not replayable by u: {source.path}")
        if reader.audit.records_read != int(source.manifest["event_count"]):
            raise RuntimeError(f"source record count mismatch: {source.path}")
        audits.append(
            sanitize_json(
                {
                    "source_relative_path": source.relative_path,
                    "source_file_id": source.source_file_id,
                    "compressed_sha256": source.fingerprint,
                    "manifest_uncompressed_sha256": source.manifest.get("sha256"),
                    "manifest_summary": {
                        key: source.manifest.get(key)
                        for key in (
                            "format_version",
                            "parser_version",
                            "compression",
                            "depth",
                            "start_utc",
                            "end_utc",
                            "event_count",
                            "native_snapshot_count",
                            "checkpoint_count",
                            "delta_count",
                            "first_u",
                            "last_u",
                            "queue_overflow",
                            "writer_errors",
                            "replayable",
                            "completion_status",
                            "continuity_status",
                            "replay_source",
                        )
                    },
                    "legacy_manifest_sequence_gap_count": len(
                        source.manifest.get("sequence_gaps") or []
                    ),
                    "manifest_u_gap_count": len(
                        source.manifest.get("u_gaps") or []
                    ),
                    "effective_contract": {
                        "continuity_field": "data.u",
                        "sequence_field": "data.seq informational only",
                        "rotation_checkpoint_is_full_snapshot": True,
                    },
                    "audit": audit,
                }
            )
        )
    return audits


def run_pilot(client: Any, window: PilotWindow) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)
    sources = discover_sources(window)
    fingerprint = input_fingerprint(
        pilot_id=window.pilot_id,
        symbol=window.symbol,
        start=window.start,
        end=window.end,
        source_fingerprints=[source.fingerprint for source in sources],
    )
    batch_id = f"phase1:{window.pilot_id}"
    if not ensure_batch_available(client, batch_id, fingerprint):
        return {
            "pilot_id": window.pilot_id,
            "batch_id": batch_id,
            "input_fingerprint": fingerprint,
            "status": "IDEMPOTENT_SKIP",
            "rows_written": 0,
        }

    source_audits = validate_sources(sources, window)
    source_trade_rows, trade_stats = read_public_trades(
        client, window.symbol, window.start, window.end
    )
    source_liq_rows, liq_stats = read_liquidations(
        client, window.symbol, window.start, window.end
    )
    oi_rows = read_open_interest(client, window.symbol, window.start, window.end)
    trade_rows = transform_trades(
        source_trade_rows,
        symbol=window.symbol,
        batch_id=batch_id,
        ingested_at=started_at,
    )
    liq_rows = transform_liquidations(
        source_liq_rows,
        symbol=window.symbol,
        batch_id=batch_id,
        ingested_at=started_at,
    )

    source_registry_rows: list[tuple] = []
    for source, audit in zip(sources, source_audits):
        if ensure_source_file_available(
            client, source.source_file_id, source.fingerprint
        ):
            source_registry_rows.append(
                (
                    source.source_file_id,
                    source.relative_path,
                    source.path.name,
                    window.symbol,
                    source.segment_start,
                    source.segment_end,
                    source.size,
                    source.manifest["compression"],
                    source.fingerprint,
                    source.manifest["parser_version"],
                    OB200_CONTRACT_VERSION,
                    source.manifest["event_count"],
                    "WINDOW_FILTERED_COMPLETE_FILE_READ",
                    batch_id,
                    audit["audit"]["first_event_time"],
                    audit["audit"]["last_event_time"],
                    "NONE_DETECTED",
                    "SEGMENT_BOUNDARY_CHECKPOINT",
                    "",
                    started_at,
                )
            )
    insert(client, "research_source_files", source_registry_rows, SOURCE_FILE_COLUMNS)

    last_event_by_second: dict[datetime, FullBookEvent] = {}
    snapshot_count = 0
    snapshot_insert_seconds = 0.0
    snapshot_buffer: list[tuple] = []
    level_sample: list[tuple] = []
    sample_events = 0
    for source in sources:
        reader = OB200SegmentReader(source, window.symbol)
        for event in reader.iter_full_books(window.start, window.end):
            snapshot_buffer.append(
                snapshot_row(
                    symbol=window.symbol,
                    event=event,
                    source=source,
                    batch_id=batch_id,
                    ingested_at=started_at,
                )
            )
            snapshot_count += 1
            last_event_by_second[event.event_time.replace(microsecond=0)] = event
            if sample_events < 10:
                for side, levels in (("bid", event.bids), ("ask", event.asks)):
                    level_sample.extend(
                        (
                            window.symbol, event.event_key, event.event_time,
                            side, rank, price, size, "PILOT_ONLY", batch_id,
                        )
                        for rank, (price, size) in enumerate(levels, 1)
                    )
                sample_events += 1
            if len(snapshot_buffer) >= 250:
                insert_started = perf_counter()
                insert(
                    client,
                    "research_orderbook_ob200_snapshots",
                    snapshot_buffer,
                    SNAPSHOT_COLUMNS,
                )
                snapshot_insert_seconds += perf_counter() - insert_started
                snapshot_buffer.clear()
        if not reader.audit.full_file_consumed:
            raise RuntimeError("import did not fully consume source file")
    insert_started = perf_counter()
    insert(
        client,
        "research_orderbook_ob200_snapshots",
        snapshot_buffer,
        SNAPSHOT_COLUMNS,
    )
    snapshot_insert_seconds += perf_counter() - insert_started
    level_insert_started = perf_counter()
    insert(client, "research_orderbook_levels_pilot", level_sample, LEVEL_COLUMNS)
    level_insert_seconds = perf_counter() - level_insert_started

    second_events = [
        event for _, event in sorted(last_event_by_second.items())
    ]
    ob_second_rows = build_orderbook_seconds(
        window.symbol,
        window.start,
        window.end,
        second_events,
        batch_id,
        started_at,
    )
    market_second_rows = build_market_seconds(
        symbol=window.symbol,
        start=window.start,
        end=window.end,
        trades=source_trade_rows,
        liquidations=liq_rows,
        oi_rows=oi_rows,
        orderbook_rows=ob_second_rows,
        batch_id=batch_id,
        ingested_at=started_at,
    )
    market_minute_rows = build_market_minutes(
        market_second_rows,
        symbol=window.symbol,
        batch_id=batch_id,
        ingested_at=started_at,
    )

    insert(client, "research_public_trades", trade_rows, TRADE_COLUMNS)
    insert(
        client,
        "research_liquidation_events",
        liq_rows,
        LIQUIDATION_COLUMNS,
    )
    insert(
        client, "research_orderbook_1s", ob_second_rows, ORDERBOOK_SECOND_COLUMNS
    )
    insert(
        client, "research_market_1s", market_second_rows, MARKET_SECOND_COLUMNS
    )
    insert(
        client, "research_market_1m", market_minute_rows, MARKET_MINUTE_COLUMNS
    )

    expected_seconds = int((window.end - window.start).total_seconds())
    coverage_rows = [
        coverage_row(
            source_id="CH_PUBLIC_TRADES_CANONICAL",
            symbol=window.symbol,
            data_type="public_trades",
            start=window.start,
            end=window.end,
            expected=trade_stats["logical_rows"],
            present=len(trade_rows),
            genuine=len(trade_rows),
            carried_forward=0,
            duplicates=0,
            batch_id=batch_id,
            checked_at=started_at,
        ),
        coverage_row(
            source_id="FS_RAW_OB200_V3",
            symbol=window.symbol,
            data_type="orderbook_1s",
            start=window.start,
            end=window.end,
            expected=expected_seconds,
            present=len(ob_second_rows),
            genuine=sum(int(row[14]) for row in ob_second_rows),
            carried_forward=sum(int(row[15]) for row in ob_second_rows),
            duplicates=0,
            batch_id=batch_id,
            checked_at=started_at,
        ),
        coverage_row(
            source_id="CH_OPEN_INTEREST_5S",
            symbol=window.symbol,
            data_type="open_interest_5s",
            start=window.start,
            end=window.end,
            expected=expected_seconds // 5,
            present=len(oi_rows),
            genuine=len(oi_rows),
            carried_forward=0,
            duplicates=0,
            batch_id=batch_id,
            checked_at=started_at,
        ),
    ]
    insert(client, "research_coverage", coverage_rows, COVERAGE_COLUMNS)

    row_counts = {
        "public_trades": len(trade_rows),
        "liquidation_events": len(liq_rows),
        "orderbook_ob200_snapshots": snapshot_count,
        "orderbook_1s": len(ob_second_rows),
        "market_1s": len(market_second_rows),
        "market_1m": len(market_minute_rows),
        "source_files_new": len(source_registry_rows),
        "pilot_normalized_levels": len(level_sample),
    }
    rows_written = sum(row_counts.values())
    completed_at = datetime.now(timezone.utc)
    manifest = {
        "pilot_id": window.pilot_id,
        "batch_id": batch_id,
        "symbol": window.symbol,
        "window_start": window.start,
        "window_end": window.end,
        "reference": window.reference,
        "input_fingerprint": fingerprint,
        "source_files": source_audits,
        "trade_source_dedup": trade_stats,
        "liquidation_source_dedup": liq_stats,
        "oi_rows": len(oi_rows),
        "row_counts": row_counts,
        "ob200_insert_elapsed_ms": round(snapshot_insert_seconds * 1000, 3),
        "normalized_sample_insert_elapsed_ms": round(
            level_insert_seconds * 1000, 3
        ),
        "normalized_sample_snapshots": sample_events,
        "status": "COMPLETE",
    }
    insert(
        client,
        "research_pipeline_state",
        [
            (
                f"{PROCESSOR_VERSION}|pilot|{window.symbol}",
                PROCESSOR_VERSION,
                "PILOT_CANONICAL_SOURCES",
                window.symbol,
                window.end,
                window.end,
                window.end,
                0,
                completed_at,
                len(source_trade_rows) + len(source_liq_rows) + snapshot_count,
                rows_written,
                PROCESSOR_VERSION,
                RESEARCH_CONTRACT_VERSION,
                "COMPLETE",
                "",
            )
        ],
        PIPELINE_COLUMNS,
    )
    insert(
        client,
        "research_ingestion_batches",
        [
            batch_row(
                batch_id=batch_id,
                fingerprint=fingerprint,
                symbol=window.symbol,
                start=window.start,
                end=window.end,
                rows_written=rows_written,
                manifest=manifest,
                started_at=started_at,
                completed_at=completed_at,
            )
        ],
        BATCH_COLUMNS,
    )
    return sanitize_json(manifest)


def preflight(client: Any) -> dict[str, Any]:
    return {
        "clickhouse_version": rows(client, "SELECT version()")[0][0],
        "target_database_preexisting": bool(
            rows(
                client,
                "SELECT name FROM system.databases WHERE name = %(database)s",
                {"database": TARGET_DATABASE},
            )
        ),
        "source_orderbook_deltas_status": "BLOCKED_SOURCE_NOT_QUERIED",
        "target_database": TARGET_DATABASE,
        "writes_allowed_only_to": f"{TARGET_DATABASE}.*",
    }


def correct_market_timezone_materialization(
    client: Any, windows: list[PilotWindow]
) -> dict[str, Any]:
    """Preserve the bad pilot tables and rebuild canonical market tables.

    This one-time correction is limited to tables created by this Phase-1 run.
    It never drops, truncates, mutates, or overwrites rows.
    """
    invalid_1s = "research_market_1s_invalid_timezone_v0"
    invalid_1m = "research_market_1m_invalid_timezone_v0"
    existing = {
        str(row[0])
        for row in rows(
            client,
            "SELECT name FROM system.tables WHERE database=%(database)s",
            {"database": TARGET_DATABASE},
        )
    }
    if invalid_1s not in existing:
        proof = rows(
            client,
            f"""
            SELECT
              (SELECT sum(trade_count) FROM {TARGET_DATABASE}.research_market_1s),
              (SELECT count() FROM {TARGET_DATABASE}.research_public_trades)
            """,
        )[0]
        if int(proof[0] or 0) != 0 or int(proof[1]) == 0:
            raise RuntimeError("timezone correction precondition not proven")
        client.command(
            f"RENAME TABLE "
            f"{TARGET_DATABASE}.research_market_1s TO "
            f"{TARGET_DATABASE}.{invalid_1s}, "
            f"{TARGET_DATABASE}.research_market_1m TO "
            f"{TARGET_DATABASE}.{invalid_1m}"
        )
        create_schema(client)
    canonical_count = int(
        rows(
            client,
            f"SELECT count() FROM {TARGET_DATABASE}.research_market_1s",
        )[0][0]
    )
    if canonical_count:
        return {
            "status": "ALREADY_CORRECTED",
            "canonical_market_1s_rows": canonical_count,
            "invalid_tables_preserved": [invalid_1s, invalid_1m],
        }

    inserted_1s = 0
    inserted_1m = 0
    for window in windows:
        corrected_at = datetime.now(timezone.utc)
        batch_id = f"phase1_timezone_correction:{window.pilot_id}"
        source_trade_rows, _ = read_public_trades(
            client, window.symbol, window.start, window.end
        )
        source_liq_rows, _ = read_liquidations(
            client, window.symbol, window.start, window.end
        )
        liq_rows = transform_liquidations(
            source_liq_rows,
            symbol=window.symbol,
            batch_id=batch_id,
            ingested_at=corrected_at,
        )
        oi_rows = read_open_interest(
            client, window.symbol, window.start, window.end
        )
        ob_rows = rows(
            client,
            f"SELECT {','.join(ORDERBOOK_SECOND_COLUMNS)} "
            f"FROM {TARGET_DATABASE}.research_orderbook_1s "
            "WHERE symbol=%(symbol)s AND bucket_time >= %(start)s "
            "AND bucket_time < %(end)s ORDER BY bucket_time",
            {
                "symbol": window.symbol,
                "start": window.start,
                "end": window.end,
            },
        )
        second_rows = build_market_seconds(
            symbol=window.symbol,
            start=window.start,
            end=window.end,
            trades=source_trade_rows,
            liquidations=liq_rows,
            oi_rows=oi_rows,
            orderbook_rows=ob_rows,
            batch_id=batch_id,
            ingested_at=corrected_at,
        )
        minute_rows = build_market_minutes(
            second_rows,
            symbol=window.symbol,
            batch_id=batch_id,
            ingested_at=corrected_at,
        )
        insert(
            client,
            "research_market_1s",
            second_rows,
            MARKET_SECOND_COLUMNS,
        )
        insert(
            client,
            "research_market_1m",
            minute_rows,
            MARKET_MINUTE_COLUMNS,
        )
        inserted_1s += len(second_rows)
        inserted_1m += len(minute_rows)
    return {
        "status": "CORRECTED",
        "cause": "clickhouse_connect UTC DateTime64 returned naive datetime",
        "invalid_tables_preserved": [invalid_1s, invalid_1m],
        "canonical_market_1s_rows": inserted_1s,
        "canonical_market_1m_rows": inserted_1m,
        "drops": 0,
        "truncates": 0,
        "mutations": 0,
        "writes_outside_target_database": 0,
    }
