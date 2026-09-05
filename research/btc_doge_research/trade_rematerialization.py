"""Controlled UTC-correct rematerialization of research_public_trades."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .clickhouse import rows
from .contracts import TARGET_DATABASE, assert_target_database
from .trade_contract import (
    BUILD_ID,
    DDL_RESEARCH_PUBLIC_TRADES_V2,
    DDL_WATERMARK,
    HISTORY_END,
    HISTORY_START,
    INVALID_SHIFTED_TABLE,
    PILOT_END,
    PILOT_START,
    PILOT_SYMBOL,
    RESULT_ROOT_NAME,
    SHIFTED_BATCH_IDS,
    TRADE_REMATERIALIZATION_CONTRACT_VERSION,
    TRADE_REMATERIALIZATION_PROCESSOR,
    WATERMARK_TABLE,
    contract_manifest,
    iso_z,
    literal_utc,
    segment_batch_id,
    source_segment_fingerprint,
)
from .trade_parity import segment_parity, shift_distribution

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULT_ROOT = REPO_ROOT / "results" / RESULT_ROOT_NAME
BACKUP_DIR = RESULT_ROOT / "backup"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_result_root() -> Path:
    RESULT_ROOT.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return RESULT_ROOT


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")


def write_csv(path: Path, fieldnames: list[str], rows_data: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows_data:
            writer.writerow(row)


def research_command(client: Any, sql: str) -> None:
    """Allow CREATE/INSERT/RENAME/ALTER DELETE only inside btc_doge_research."""
    stripped = sql.strip().lstrip("(")
    upper = stripped.upper()
    if upper.startswith("INSERT INTO"):
        target = stripped.split(None, 2)[2].split("(", 1)[0].strip().replace("`", "")
        assert_target_database(target.split(".", 1)[0])
    elif upper.startswith("CREATE TABLE") or upper.startswith("CREATE DATABASE"):
        parts = stripped.replace("`", "").split()
        if upper.startswith("CREATE DATABASE"):
            idx = parts.index("DATABASE") + 1
        else:
            idx = parts.index("TABLE") + 1
        if idx < len(parts) and parts[idx].upper() == "IF":
            # IF NOT EXISTS
            idx += 3
        target = parts[idx]
        assert_target_database(target.split(".", 1)[0])
    elif upper.startswith("RENAME TABLE"):
        for token in stripped.replace("`", "").replace(",", " ").split():
            if "." in token and token.split(".", 1)[0] == TARGET_DATABASE:
                continue
            if token.upper() in {"RENAME", "TABLE", "TO"}:
                continue
            if "." in token:
                assert_target_database(token.split(".", 1)[0])
    elif upper.startswith("ALTER TABLE"):
        parts = stripped.replace("`", "").split()
        target = parts[2]
        assert_target_database(target.split(".", 1)[0])
        if "DELETE" not in upper:
            raise PermissionError("only ALTER TABLE ... DELETE allowed for rematerialization")
        # Require exact batch or build predicates — no bare DELETE without WHERE.
        if "WHERE" not in upper:
            raise PermissionError("ALTER DELETE requires WHERE")
    else:
        raise PermissionError(f"unsupported rematerialization write: {sql[:80]}")
    client.command(sql)


def hour_segments(
    start: datetime = HISTORY_START,
    end: datetime = HISTORY_END,
) -> Iterator[tuple[datetime, datetime]]:
    cursor = start
    while cursor < end:
        nxt = min(cursor + timedelta(hours=1), end)
        yield cursor, nxt
        cursor = nxt


def source_stats(client: Any, symbol: str, start: datetime, end: datetime) -> dict[str, Any]:
    a, b = literal_utc(start), literal_utc(end)
    row = rows(
        client,
        f"""
        SELECT count(), uniqExact(trade_id),
               min(trade_id), max(trade_id),
               min(trade_ts), max(trade_ts)
        FROM orderbook_analysis.public_trades_canonical
        WHERE symbol = %(symbol)s
          AND trade_ts >= toDateTime64(%(a)s, 3, 'UTC')
          AND trade_ts < toDateTime64(%(b)s, 3, 'UTC')
        """,
        {"symbol": symbol, "a": a, "b": b},
    )[0]
    return {
        "source_row_count": int(row[0]),
        "source_unique_trade_ids": int(row[1]),
        "min_trade_id": "" if row[2] is None else str(row[2]),
        "max_trade_id": "" if row[3] is None else str(row[3]),
        "min_ts": row[4],
        "max_ts": row[5],
    }


def table_exists(client: Any, name: str) -> bool:
    return bool(
        rows(
            client,
            "SELECT count() FROM system.tables WHERE database=%(db)s AND name=%(n)s",
            {"db": TARGET_DATABASE, "n": name},
        )[0][0]
    )


def current_trades_engine(client: Any) -> dict[str, Any]:
    if not table_exists(client, "research_public_trades"):
        return {"exists": False}
    meta = rows(
        client,
        """
        SELECT engine, sorting_key, partition_key, total_rows, create_table_query
        FROM system.tables
        WHERE database=%(db)s AND name='research_public_trades'
        """,
        {"db": TARGET_DATABASE},
    )[0]
    cols = rows(
        client,
        """
        SELECT name, type FROM system.columns
        WHERE database=%(db)s AND table='research_public_trades'
        ORDER BY position
        """,
        {"db": TARGET_DATABASE},
    )
    return {
        "exists": True,
        "engine": meta[0],
        "sorting_key": meta[1],
        "partition_key": meta[2],
        "total_rows": int(meta[3]),
        "columns": [{"name": n, "type": t} for n, t in cols],
        "create_table_query": meta[4],
        "has_record_version": any(n == "record_version" for n, _ in cols),
        "is_v2": any(n == "record_version" for n, _ in cols)
        and "ReplacingMergeTree" in str(meta[0]),
    }


def quarantine_shifted_table(client: Any) -> dict[str, Any]:
    """Rename shifted MergeTree aside; create empty v2 canonical table."""
    ensure_result_root()
    info = current_trades_engine(client)
    if info.get("is_v2"):
        return {"status": "ALREADY_V2", "engine": info}
    if not info.get("exists"):
        research_command(client, DDL_RESEARCH_PUBLIC_TRADES_V2)
        research_command(client, DDL_WATERMARK)
        return {"status": "CREATED_EMPTY_V2"}
    if table_exists(client, INVALID_SHIFTED_TABLE):
        # Canonical name missing? recreate v2 only if needed
        if not info.get("exists"):
            research_command(client, DDL_RESEARCH_PUBLIC_TRADES_V2)
        research_command(client, DDL_WATERMARK)
        return {"status": "INVALID_ALREADY_PRESENT", "engine": info}
    # Preserve shifted rows under audit name, then create v2.
    research_command(
        client,
        f"RENAME TABLE {TARGET_DATABASE}.research_public_trades "
        f"TO {TARGET_DATABASE}.{INVALID_SHIFTED_TABLE}",
    )
    research_command(client, DDL_RESEARCH_PUBLIC_TRADES_V2)
    research_command(client, DDL_WATERMARK)
    return {
        "status": "QUARANTINED_AND_CREATED_V2",
        "invalid_table": INVALID_SHIFTED_TABLE,
        "previous": info,
    }


def fingerprint_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup_shifted_state(client: Any) -> dict[str, Any]:
    """Export shifted research trades + related batch rows before mutation."""
    ensure_result_root()
    stamp = _now().strftime("%Y%m%dT%H%M%SZ")
    backup_root = BACKUP_DIR / stamp
    backup_root.mkdir(parents=True, exist_ok=True)

    trades_table = (
        INVALID_SHIFTED_TABLE
        if table_exists(client, INVALID_SHIFTED_TABLE)
        else "research_public_trades"
    )
    trades_path = backup_root / "research_public_trades_shifted.parquet"
    batches_path = backup_root / "research_ingestion_batches.parquet"
    probe_path = backup_root / "tz_probe_trades.parquet"

    def _export(table: str, dest: Path, where: str = "") -> dict[str, Any]:
        if not table_exists(client, table) and table != "research_public_trades":
            # may be fully qualified invalid name as short name
            pass
        full = f"{TARGET_DATABASE}.{table}"
        count = int(rows(client, f"SELECT count() FROM {full} {where}")[0][0])
        if count == 0:
            return {"table": full, "rows": 0, "path": None}
        # Native Parquet via clickhouse-connect query + pyarrow
        import pyarrow as pa
        import pyarrow.parquet as pq

        result = client.query(f"SELECT * FROM {full} {where}")
        arrays = [pa.array(col) for col in result.result_columns]
        table_pa = pa.Table.from_arrays(arrays, names=result.column_names)
        pq.write_table(table_pa, dest)
        return {
            "table": full,
            "rows": count,
            "path": str(dest.relative_to(REPO_ROOT)),
            "sha256": fingerprint_file(dest),
            "bytes": dest.stat().st_size,
        }

    exports = [
        _export(trades_table, trades_path),
        _export(
            "research_ingestion_batches",
            batches_path,
            where="WHERE batch_id IN ('phase1:btc_run_018','phase1:doge_20260829_1145_1230')",
        ),
    ]
    if table_exists(client, "_tz_probe_trades"):
        exports.append(_export("_tz_probe_trades", probe_path))

    # Row fingerprints
    fp_rows = rows(
        client,
        f"""
        SELECT symbol, count(), uniqExact(trade_id),
               min(event_time), max(event_time),
               hex(sipHash128(groupArray(trade_id)))
        FROM {TARGET_DATABASE}.{trades_table}
        GROUP BY symbol ORDER BY symbol
        """,
    )
    symbol_fp = [
        {
            "symbol": r[0],
            "rows": int(r[1]),
            "unique_trade_ids": int(r[2]),
            "min_ts": iso_z(r[3].replace(tzinfo=timezone.utc)),
            "max_ts": iso_z(r[4].replace(tzinfo=timezone.utc)),
            "trade_id_siphash": r[5],
        }
        for r in fp_rows
    ]
    manifest = {
        "created_at": iso_z(_now()),
        "backup_dir": str(backup_root.relative_to(REPO_ROOT)),
        "exports": exports,
        "symbol_fingerprints": symbol_fp,
        "shifted_batch_ids": list(SHIFTED_BATCH_IDS),
        "restore": {
            "instruction": (
                f"To restore pre-repair shifted rows: ensure "
                f"{TARGET_DATABASE}.{INVALID_SHIFTED_TABLE} exists (or recreate from parquet). "
                "Do not copy shifted rows back into the canonical research_public_trades view."
            ),
            "parquet": str(trades_path.relative_to(REPO_ROOT)),
        },
    }
    write_json(RESULT_ROOT / "backup_manifest.json", manifest)
    write_json(backup_root / "backup_manifest.json", manifest)
    return manifest


def build_plan(client: Any) -> dict[str, Any]:
    ensure_result_root()
    engine = current_trades_engine(client)
    segments = []
    total_source = 0
    a, b = literal_utc(HISTORY_START), literal_utc(HISTORY_END)
    for symbol in ("BTCUSDT", "DOGEUSDT"):
        hour_rows = rows(
            client,
            """
            SELECT
              toStartOfHour(trade_ts) AS h,
              count() AS source_row_count,
              uniqExact(trade_id) AS source_unique_trade_ids
            FROM orderbook_analysis.public_trades_canonical
            WHERE symbol = %(symbol)s
              AND trade_ts >= toDateTime64(%(a)s, 3, 'UTC')
              AND trade_ts < toDateTime64(%(b)s, 3, 'UTC')
            GROUP BY h
            ORDER BY h
            """,
            {"symbol": symbol, "a": a, "b": b},
        )
        by_hour = {
            (
                r[0].replace(tzinfo=timezone.utc)
                if r[0].tzinfo is None
                else r[0].astimezone(timezone.utc)
            ): r
            for r in hour_rows
        }
        for start, end in hour_segments():
            st = by_hour.get(start)
            source_rows = int(st[1]) if st else 0
            source_unique = int(st[2]) if st else 0
            total_source += source_unique
            segments.append(
                {
                    "symbol": symbol,
                    "segment_start": iso_z(start),
                    "segment_end": iso_z(end),
                    "source_unique_trade_ids": source_unique,
                    "source_row_count": source_rows,
                    "planned_status": "IMPORT" if source_unique else "EMPTY_SOURCE",
                }
            )
    plan = {
        "architecture": "B_VERSIONED_REPLACING_MERGETREE",
        "contract": contract_manifest(),
        "current_engine": engine,
        "segment_count": len(segments),
        "nonempty_segments": sum(1 for s in segments if s["source_unique_trade_ids"]),
        "approx_unique_trades": total_source,
        "pilot": {
            "symbol": PILOT_SYMBOL,
            "start": iso_z(PILOT_START),
            "end": iso_z(PILOT_END),
        },
    }
    write_json(RESULT_ROOT / "repair_plan.json", plan)
    write_csv(
        RESULT_ROOT / "rematerialization_segments_plan.csv",
        list(segments[0].keys()),
        segments,
    )
    return plan


def write_rollback_plan() -> Path:
    ensure_result_root()
    path = RESULT_ROOT / "rollback_plan.md"
    path.write_text(
        f"""# Rollback plan — research public trades rematerialization

Quarantine table: `{TARGET_DATABASE}.{INVALID_SHIFTED_TABLE}`
Backup: `results/{RESULT_ROOT_NAME}/backup/`

Do not copy shifted rows back into canonical `research_public_trades`.
"""
    )
    return path


def write_downstream_lineage_audit(client: Any) -> dict[str, Any]:
    ensure_result_root()
    rows_out = []

    def add(table: str, **kwargs: Any) -> None:
        rows_out.append({"table": table, **kwargs})

    for suffix in ("100ms", "500ms", "1s"):
        table = f"research_public_trade_buckets_{suffix}"
        cnt = int(rows(client, f"SELECT count() FROM {TARGET_DATABASE}.{table}")[0][0])
        add(
            table,
            source_table="orderbook_analysis.public_trades_canonical",
            classification="VALID_BUT_EXTERNAL_LINEAGE",
            rows=cnt,
            notes="SQL from OA; Fight CLI builds TPO/Volume from research events causally",
        )
    for table in (
        "research_tpo_profile_bins_session",
        "research_volume_profile_bins_session",
        "research_tpo_bracket_ranges_30m",
    ):
        cnt = int(rows(client, f"SELECT count() FROM {TARGET_DATABASE}.{table}")[0][0])
        add(
            table,
            source_table="orderbook_analysis.public_trades_canonical",
            classification="NOT_USED_BY_FIGHT_CLI",
            rows=cnt,
            notes="Fight CLI builds profiles from research_public_trades events; mark non-decisionable",
        )
    write_csv(
        RESULT_ROOT / "downstream_lineage_audit.csv",
        ["table", "source_table", "classification", "rows", "notes"],
        rows_out,
    )
    rebuild = {
        "fight_cli_profiles": "BUILT_CAUSALLY_FROM_RESEARCH_PUBLIC_TRADES_EVENTS",
        "derived_tpo_volume_tables": "NOT_DECISIONABLE_NO_FORCED_REBUILD",
        "research_public_trades": "REMATERIALIZE_V2_SERVER_SIDE",
    }
    write_json(RESULT_ROOT / "downstream_rebuild_plan.json", rebuild)
    return {"rows": rows_out, "rebuild": rebuild}
