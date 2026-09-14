"""Guarded, resumable CLI for a separately authorized BTC Silver v1.3 full build.

Inert unless ``--run`` is supplied. ``--check-only`` and ``--verify-only`` never
execute Silver DML. Bronze input must be fully verified and no Bronze import
may be active before any Silver work begins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .epoch_aware_silver_v1_3 import (
    EpochDefinition,
    EpochDiscovery,
    EpochSilverError,
    GapDefinition,
    INSERT_BATCH_MAX_BYTES,
    INSERT_BATCH_SIZE,
    ReplayResult,
    _canonical_bytes,
    _check_rss,
    _hash,
    _record_key,
    _text,
    analysis_bucket_count,
    clean_segment_start_ranks,
    discover_epochs,
    epoch_apply_bounds,
    level_change_row_id,
    make_build_id,
    make_chunk_key,
    replay_epoch_window,
    split_resume_and_replay_stream,
    state_row_id,
    validate_epoch_window,
)
from .helpers import (
    client_session_id,
    current_rss_bytes,
    get_clickhouse_client,
    iso_to_ns_exact,
)
from .silver_replay import BronzeRecord, bronze_row_from_ch

EXPECTED_BRANCH = "research/clickhouse-defense-store-v1"
EXPECTED_SEGMENT_COUNT = 164
EXPECTED_BRONZE_RECORDS = 2_638_997
DEFAULT_INPUT_DATABASE = "research_full_ob_continuous_v1_3"
DEFAULT_OUTPUT_DATABASE = "research_full_ob_silver_v1_3"
BRONZE_SEGMENTS_TABLE = "canonical_segments_v1_3"
BRONZE_EVENTS_TABLE = "raw_full_ob_events_v1_3"
BRONZE_LEDGER_TABLE = "raw_full_ob_import_segments_v1_3"
EPOCHS_TABLE = "replay_epochs_v1_3"
LEVEL_CHANGES_TABLE = "ob_level_changes_v1_3"
METRICS_TABLE = "ob_metrics_100ms_v1_3"
CHUNKS_TABLE = "silver_build_chunks_v1_3"
RUNS_TABLE = "silver_build_runs_v1_3"
GAPS_TABLE = "silver_epoch_gaps_v1_3"
RUNNER_VERSION = "btc_silver_full_build_v1_3"
BUILD_SCHEMA_VERSION = "silver_full_build_v1_3"
DEFAULT_CHUNK_MARKET_MINUTES = 15
DEFAULT_WARMUP_MINUTES = 0
DEFAULT_MAX_RSS_MIB = 1536
DEFAULT_MIN_FREE_DISK_GIB = 200.0
DEFAULT_MIN_AVAILABLE_MEMORY_MIB = 4096
DEFAULT_BRONZE_LOCK_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/bronze_full_import_v1_3/import.lock"
)
DEFAULT_LOCK_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/silver_full_build_v1_3/build.lock"
)
DEFAULT_REPAIR_LOCK_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/repair.lock"
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CHUNK_STATUSES = frozenset(
    {
        "QUEUED",
        "RUNNING",
        "COMPLETE",
        "INTERRUPTED",
        "FAILED",
        "SKIPPED_ALREADY_COMPLETE",
        "EPOCH_BOUNDARY",
    }
)
BRONZE_STREAM_MAX_MEMORY_BYTES = 1_610_612_736  # 1.5 GiB per Silver query
BRONZE_STREAM_MAX_BLOCK_SIZE = 8192
BRONZE_STREAM_SETTINGS = {
    "max_threads": 1,
    "max_block_size": BRONZE_STREAM_MAX_BLOCK_SIZE,
    "max_memory_usage": BRONZE_STREAM_MAX_MEMORY_BYTES,
}
BRONZE_PAYLOAD_FULL = "full"
BRONZE_PAYLOAD_EPOCH_PLAN = "epoch_plan"


class SilverBuildError(RuntimeError):
    """Fail-closed Silver runner error with a stable STOP verdict."""


class ControlledInterrupt(SilverBuildError):
    """Raised after SIGINT/SIGTERM requests a controlled stop."""


@dataclass
class SilverSessionClients:
    """Dedicated ClickHouse clients so Bronze streams never lock Silver DML.

    ``read`` owns Bronze ``query_row_block_stream`` results.
    ``write`` owns Silver inserts and chunk-ledger updates.
    ``verify`` owns post-insert count/hash checks and resource probes.
    """

    read: Any
    write: Any
    verify: Any
    shared_legacy: bool = False

    def session_ids(self) -> dict[str, str]:
        return {
            "read": client_session_id(self.read),
            "write": client_session_id(self.write),
            "verify": client_session_id(self.verify),
        }

    def assert_isolated(self) -> None:
        if self.shared_legacy:
            return
        ids = self.session_ids()
        read_id, write_id, verify_id = ids["read"], ids["write"], ids["verify"]
        if not read_id or not write_id or not verify_id:
            raise SilverBuildError(
                "STOP_SILVER_CH_SESSION_LOCKED: missing session_id on isolated clients"
            )
        if len({read_id, write_id, verify_id}) != 3:
            raise SilverBuildError(
                "STOP_SILVER_CH_SESSION_LOCKED: read/write/verify sessions are not unique "
                f"(read={read_id}, write={write_id}, verify={verify_id})"
            )
        if self.read is self.write or self.read is self.verify or self.write is self.verify:
            raise SilverBuildError(
                "STOP_SILVER_CH_SESSION_LOCKED: read/write/verify must be distinct clients"
            )

    def close(self) -> None:
        seen: set[int] = set()
        for client in (self.read, self.write, self.verify):
            marker = id(client)
            if marker in seen:
                continue
            seen.add(marker)
            closer = getattr(client, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001
                    pass


def open_silver_session_clients() -> SilverSessionClients:
    """Create three role-scoped clients with distinct ClickHouse sessions."""
    clients = SilverSessionClients(
        read=get_clickhouse_client(role="read"),
        write=get_clickhouse_client(role="write"),
        verify=get_clickhouse_client(role="verify"),
        shared_legacy=False,
    )
    clients.assert_isolated()
    return clients


def _as_session_clients(client_or_bundle: Any) -> SilverSessionClients:
    if isinstance(client_or_bundle, SilverSessionClients):
        return client_or_bundle
    return SilverSessionClients(
        read=client_or_bundle,
        write=client_or_bundle,
        verify=client_or_bundle,
        shared_legacy=True,
    )


def _close_streaming_iterator(iterator: Any) -> None:
    """Close a Bronze generator so its ``query_row_block_stream`` context exits."""
    closer = getattr(iterator, "close", None)
    if not callable(closer):
        return
    try:
        closer()
    except Exception:  # noqa: BLE001
        pass


@dataclass(frozen=True)
class BuildConfig:
    symbol: str
    input_database: str
    output_database: str
    chain_version: str
    expected_chain_hash: str
    expected_bronze_records: int
    resume: bool
    start_chain_index: int
    end_chain_index: int
    chunk_market_minutes: int
    warmup_minutes: int
    max_rss_mib: int
    min_free_disk_gib: float
    min_available_memory_mib: int
    progress_every_chunks: int
    report_path: Path
    lock_path: Path
    bronze_lock_path: Path = DEFAULT_BRONZE_LOCK_PATH
    expected_segment_count: int = EXPECTED_SEGMENT_COUNT
    enforce_canonical_lock_path: bool = True


@dataclass
class ChunkPlan:
    epoch_index: int
    chunk_index: int
    epoch: EpochDefinition
    analysis_start_ns: int
    analysis_end_ns: int
    warmup_ns: int
    build_id: str = ""
    chunk_key: str = ""

    def materialize_ids(self) -> None:
        self.build_id = make_build_id(
            chain_version=self.epoch.chain_version,
            canonical_chain_hash=self.epoch.canonical_chain_hash,
            epoch_hash=self.epoch.epoch_hash,
            symbol=self.epoch.symbol,
            analysis_start_ns=self.analysis_start_ns,
            analysis_end_ns=self.analysis_end_ns,
        )
        self.chunk_key = make_chunk_key(
            build_id=self.build_id,
            epoch_id=self.epoch.epoch_id,
            epoch_hash=self.epoch.epoch_hash,
            chunk_start_ns=self.analysis_start_ns,
            chunk_end_ns=self.analysis_end_ns,
            warmup_ns=self.warmup_ns,
        )


@dataclass
class BuildPlan:
    epochs: list[EpochDefinition]
    gaps: list[GapDefinition]
    chunks: list[ChunkPlan] = field(default_factory=list)
    epoch_plan_hash: str = ""
    clean_segment_start_ranks: set[int] = field(default_factory=set)

    def finalize(self) -> None:
        material = {
            "epochs": [epoch.stable_payload() for epoch in self.epochs],
            "gaps": [asdict(gap) for gap in self.gaps],
        }
        self.epoch_plan_hash = _hash(material)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ns_iso(ns: int) -> str:
    seconds, remainder = divmod(int(ns), 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}.{remainder:09d}Z"


def _sha_text(value: str, *, label: str) -> str:
    normalized = str(value).lower()
    if len(normalized) != 64 or any(ch not in "0123456789abcdef" for ch in normalized):
        raise SilverBuildError(f"STOP_SILVER_RUNNER_SAFETY_{label}_INVALID")
    return normalized


def code_identity(repo_root: Path | None = None) -> dict[str, str]:
    root = repo_root or Path(__file__).resolve().parents[4]

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()

    try:
        return {"branch": git("branch", "--show-current"), "head": git("rev-parse", "HEAD")}
    except (OSError, subprocess.CalledProcessError):
        return {"branch": "UNKNOWN", "head": "UNKNOWN"}


def validate_input_database(database: str) -> None:
    if not _IDENTIFIER.fullmatch(database):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_INPUT_DATABASE_IDENTIFIER")
    if database != DEFAULT_INPUT_DATABASE:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_INPUT_DATABASE_NOT_PRODUCTION_V1_3")


def validate_output_database(database: str) -> None:
    if not _IDENTIFIER.fullmatch(database):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_OUTPUT_DATABASE_IDENTIFIER")
    if database != DEFAULT_OUTPUT_DATABASE:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_OUTPUT_DATABASE_NOT_PRODUCTION_V1_3")


def _table_ddls(database: str) -> tuple[str, ...]:
    validate_output_database(database)
    return (
        f"CREATE DATABASE IF NOT EXISTS {database}",
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{EPOCHS_TABLE}
        (
            epoch_id FixedString(64),
            epoch_hash FixedString(64),
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            anchor_type LowCardinality(String),
            anchor_provenance String,
            anchor_event_time_ns UInt64,
            anchor_event_time DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(anchor_event_time_ns, 'UTC'),
            anchor_receive_time_ns UInt64,
            anchor_receive_time DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(anchor_receive_time_ns, 'UTC'),
            anchor_u UInt64,
            anchor_seq UInt64,
            anchor_segment_chain_index UInt64,
            anchor_record_ordinal UInt64,
            safe_start_ns UInt64,
            safe_start DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(safe_start_ns, 'UTC'),
            safe_end_ns UInt64,
            safe_end DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(safe_end_ns, 'UTC'),
            terminating_reason LowCardinality(String),
            preceding_gap_id String,
            status LowCardinality(String),
            reconnect_resync_proven UInt8,
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY (chain_version, epoch_id)
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{GAPS_TABLE}
        (
            gap_id FixedString(64),
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            reason LowCardinality(String),
            segment_chain_index UInt64,
            record_ordinal UInt64,
            gap_time_ns UInt64,
            gap_time DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(gap_time_ns, 'UTC'),
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY (chain_version, gap_id)
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{LEVEL_CHANGES_TABLE}
        (
            row_id FixedString(64),
            build_id FixedString(64),
            chunk_key FixedString(64),
            epoch_id FixedString(64),
            epoch_hash FixedString(64),
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            canonical_segment_chain_index UInt64,
            source_segment_sha256 FixedString(64),
            record_ordinal UInt64,
            apply_order UInt64,
            event_time_ns UInt64,
            event_time DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(event_time_ns, 'UTC'),
            receive_time_ns UInt64,
            receive_time DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(receive_time_ns, 'UTC'),
            payload String,
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY (build_id, chunk_key, row_id)
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{METRICS_TABLE}
        (
            row_id FixedString(64),
            build_id FixedString(64),
            chunk_key FixedString(64),
            epoch_id FixedString(64),
            epoch_hash FixedString(64),
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            bucket_start_ns UInt64,
            bucket_start DateTime64(9, 'UTC')
                MATERIALIZED fromUnixTimestamp64Nano(bucket_start_ns, 'UTC'),
            payload String,
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY (build_id, chunk_key, row_id)
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{CHUNKS_TABLE}
        (
            chunk_key FixedString(64),
            build_id FixedString(64),
            run_id FixedString(64),
            epoch_id FixedString(64),
            epoch_hash FixedString(64),
            epoch_plan_hash FixedString(64),
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            chunk_start_ns UInt64,
            chunk_end_ns UInt64,
            warmup_ns UInt64,
            status LowCardinality(String),
            stop_reason String,
            level_change_count UInt64,
            state_count UInt64,
            source_record_count UInt64,
            output_hash FixedString(64),
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY chunk_key
        SETTINGS index_granularity = 8192
        """.strip(),
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{RUNS_TABLE}
        (
            run_id FixedString(64),
            runner_version String,
            build_schema_version String,
            chain_version String,
            canonical_chain_hash FixedString(64),
            epoch_plan_hash FixedString(64),
            symbol LowCardinality(String),
            input_database String,
            output_database String,
            status LowCardinality(String),
            chunk_total UInt64,
            chunk_complete UInt64,
            build_hash FixedString(64),
            host String,
            pid UInt64,
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY run_id
        SETTINGS index_granularity = 8192
        """.strip(),
    )


def init_schema(client: Any, database: str) -> None:
    client.command("SET max_threads = 1")
    for ddl in _table_ddls(database):
        client.command(ddl)


def target_schema_exists(client: Any, database: str) -> bool:
    tables = [
        EPOCHS_TABLE,
        GAPS_TABLE,
        LEVEL_CHANGES_TABLE,
        METRICS_TABLE,
        CHUNKS_TABLE,
        RUNS_TABLE,
    ]
    count = client.query(
        """
        SELECT count()
        FROM system.tables
        WHERE database = {database:String}
          AND name IN {tables:Array(String)}
        """,
        parameters={"database": database, "tables": tables},
    ).result_rows[0][0]
    return int(count) == len(tables)


def _available_memory_bytes() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            info = {}
            for line in handle:
                key, value = line.split(":", 1)
                info[key.strip()] = int(value.strip().split()[0]) * 1024
        return int(info.get("MemAvailable", 0))
    except OSError:
        return 0


def clickhouse_free_bytes(client: Any) -> int:
    rows = client.query(
        "SELECT min(free_space) FROM system.disks WHERE type != 'ObjectStorage'"
    ).result_rows
    if not rows or rows[0][0] is None:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_DISK_SPACE_UNKNOWN")
    return int(rows[0][0])


def _check_resources(client: Any, config: BuildConfig) -> tuple[int, int, int]:
    if config.max_rss_mib <= 0 or config.min_free_disk_gib <= 0:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_RESOURCE_LIMIT_INVALID")
    if config.min_available_memory_mib <= 0:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_RESOURCE_LIMIT_INVALID")
    rss = current_rss_bytes()
    if rss > config.max_rss_mib * 1024 * 1024:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_RSS_LIMIT")
    free = clickhouse_free_bytes(client)
    if free < int(config.min_free_disk_gib * 1024**3):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_FREE_DISK_LIMIT")
    available = _available_memory_bytes()
    if available < int(config.min_available_memory_mib * 1024 * 1024):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_MEMORY_LIMIT")
    return rss, free, available


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def detect_bronze_import_running(
    *,
    bronze_lock_path: Path = DEFAULT_BRONZE_LOCK_PATH,
    process_probe: Any | None = None,
) -> dict[str, Any]:
    probe = process_probe if process_probe is not None else _default_bronze_process_probe
    running_pids = probe()
    lock_info: dict[str, Any] | None = None
    if bronze_lock_path.exists():
        try:
            lock_info = json.loads(bronze_lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lock_info = {"status": "UNREADABLE"}
        else:
            pid = int(lock_info.get("pid") or 0)
            lock_info["pid_alive"] = pid > 0 and _pid_alive(pid)
    active = bool(running_pids) or bool(
        lock_info and lock_info.get("pid_alive") is True
    )
    return {
        "active": active,
        "running_pids": running_pids,
        "lock_path": str(bronze_lock_path),
        "lock": lock_info,
    }


def _default_bronze_process_probe() -> list[int]:
    try:
        output = subprocess.check_output(
            ["pgrep", "-f", "bronze_full_import_v1_3.*--run"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return []
    pids = [int(line.strip()) for line in output.splitlines() if line.strip().isdigit()]
    return [pid for pid in pids if pid != os.getpid()]


def load_segment_metadata(
    client: Any,
    config: BuildConfig,
) -> dict[int, dict[str, Any]]:
    rows = client.query(
        f"""
        SELECT
          canonical_segment_chain_index, source_segment_sha256, source_path,
          segment_start_ns, segment_end_ns, first_archive_time_ns,
          last_archive_time_ns, first_event_time_ns, last_event_time_ns,
          first_receive_time_ns, last_receive_time_ns, archive_instance_id,
          record_count, resolution_status, is_canonical,
          predecessor_segment_sha256, anchor_type, anchor_provenance,
          continuity_status, canonical_chain_hash
        FROM {config.input_database}.{BRONZE_SEGMENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND canonical_chain_hash = {{chain_hash:String}}
          AND is_canonical = 1
          AND canonical_segment_chain_index >= {{start_index:UInt64}}
          AND canonical_segment_chain_index <= {{end_index:UInt64}}
        ORDER BY canonical_segment_chain_index
        """,
        parameters={
            "symbol": config.symbol,
            "chain_version": config.chain_version,
            "chain_hash": config.expected_chain_hash,
            "start_index": config.start_chain_index,
            "end_index": config.end_chain_index,
        },
    ).result_rows
    metadata: dict[int, dict[str, Any]] = {}
    for row in rows:
        rank = int(row[0])
        if rank in metadata or _text(row[19]) != config.expected_chain_hash:
            raise SilverBuildError(
                "STOP_SILVER_RUNNER_SAFETY_BRONZE_MAPPING_INVALID"
            )
        metadata[rank] = {
            "canonical_segment_chain_index": rank,
            "source_segment_sha256": _text(row[1]),
            "source_path": _text(row[2]),
            "segment_start_ns": int(row[3]),
            "segment_end_ns": int(row[4]),
            "first_archive_time_ns": int(row[5]),
            "last_archive_time_ns": int(row[6]),
            "first_event_time_ns": int(row[7]),
            "last_event_time_ns": int(row[8]),
            "first_receive_time_ns": int(row[9]),
            "last_receive_time_ns": int(row[10]),
            "archive_instance_id": _text(row[11]),
            "record_count": int(row[12]),
            "resolution_status": _text(row[13]),
            "is_canonical": int(row[14]),
            "predecessor_segment_sha256": _text(row[15]),
            "anchor_type": _text(row[16]),
            "anchor_provenance": _text(row[17]),
            "continuity_status": _text(row[18]),
        }
    expected = list(range(config.start_chain_index, config.end_chain_index + 1))
    if sorted(metadata) != expected:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_BRONZE_SEGMENT_RANKS_INVALID")
    return metadata


def bronze_hard_preflight(
    client: Any,
    config: BuildConfig,
    *,
    bronze_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    probe = bronze_probe if bronze_probe is not None else detect_bronze_import_running(
        bronze_lock_path=config.bronze_lock_path
    )
    if probe.get("active"):
        raise SilverBuildError("STOP_SILVER_BRONZE_INPUT_NOT_READY: bronze import active")

    metadata = load_segment_metadata(client, config)
    if len(metadata) != config.expected_segment_count:
        raise SilverBuildError("STOP_SILVER_BRONZE_INPUT_NOT_READY: segment count")

    ledger = client.query(
        f"""
        SELECT status, count()
        FROM {config.input_database}.{BRONZE_LEDGER_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
          AND canonical_chain_hash = {{chain_hash:String}}
        GROUP BY status
        ORDER BY status
        """,
        parameters={
            "symbol": config.symbol,
            "chain_version": config.chain_version,
            "chain_hash": config.expected_chain_hash,
        },
    ).result_rows
    status_counts = {_text(row[0]): int(row[1]) for row in ledger}
    if status_counts.get("COMPLETE", 0) != config.expected_segment_count:
        raise SilverBuildError("STOP_SILVER_BRONZE_INPUT_NOT_READY: incomplete bronze ledger")
    for bad in ("FAILED", "INTERRUPTED", "RUNNING", "RESUMING"):
        if status_counts.get(bad, 0):
            raise SilverBuildError(
                f"STOP_SILVER_BRONZE_INPUT_NOT_READY: bronze ledger status {bad}"
            )

    event_row = client.query(
        f"""
        SELECT
          count(),
          uniqExact(record_id),
          uniqExact((source_segment_sha256, record_ordinal)),
          uniqExact(canonical_segment_chain_index),
          uniqExact(source_segment_sha256),
          countIf(
            toUnixTimestamp64Nano(event_time) != event_time_ns
            OR toUnixTimestamp64Nano(receive_time) != receive_time_ns
          ),
          countIf(
            record_id != lower(hex(SHA256(concat(
              toString(source_segment_sha256), ':', toString(record_ordinal)
            ))))
          )
        FROM {config.input_database}.{BRONZE_EVENTS_TABLE} FINAL
        WHERE symbol = {{symbol:String}}
          AND chain_version = {{chain_version:String}}
        """,
        parameters={
            "symbol": config.symbol,
            "chain_version": config.chain_version,
        },
    ).result_rows[0]
    total, unique_ids, unique_pairs, unique_ranks, unique_shas, utc_bad, bad_ids = (
        int(event_row[0]),
        int(event_row[1]),
        int(event_row[2]),
        int(event_row[3]),
        int(event_row[4]),
        int(event_row[5]),
        int(event_row[6]),
    )
    expected_records = sum(row["record_count"] for row in metadata.values())
    if (
        total != config.expected_bronze_records
        or expected_records != config.expected_bronze_records
        or total != unique_ids
        or total != unique_pairs
        or unique_ranks != config.expected_segment_count
        or unique_shas != config.expected_segment_count
        or utc_bad
        or bad_ids
    ):
        raise SilverBuildError("STOP_SILVER_BRONZE_INPUT_NOT_READY: bronze event contract")

    return {
        "status": "PASS",
        "segments": len(metadata),
        "records": total,
        "unique_segment_shas": unique_shas,
        "unique_chain_indices": unique_ranks,
        "utc_ns_mismatches": utc_bad,
        "duplicate_record_ids": bad_ids,
        "ledger_status_counts": status_counts,
        "bronze_probe": probe,
    }


def _is_clickhouse_memory_error(exc: BaseException) -> bool:
    text = str(exc)
    return "241" in text or "MEMORY_LIMIT_EXCEEDED" in text


def _is_clickhouse_session_locked(exc: BaseException) -> bool:
    text = str(exc)
    return (
        "SESSION_IS_LOCKED" in text
        or "Code: 373" in text
        or "code: 373" in text
        or "STOP_SILVER_CH_SESSION_LOCKED" in text
    )


def _raise_clickhouse_stream_error(exc: BaseException) -> None:
    if _is_clickhouse_memory_error(exc):
        raise SilverBuildError(f"STOP_SILVER_CH_MEMORY_LIMIT: {exc}") from exc
    if _is_clickhouse_session_locked(exc):
        raise SilverBuildError(f"STOP_SILVER_CH_SESSION_LOCKED: {exc}") from exc
    raise SilverBuildError(f"STOP_SILVER_CH_QUERY_NOT_STREAMING: {exc}") from exc


def _is_python_memory_limit(exc: BaseException) -> bool:
    text = str(exc)
    return (
        "STOP_SILVER_MEMORY_LIMIT" in text
        or "STOP_SILVER_RUNNER_SAFETY_RSS_LIMIT" in text
        or "STOP_SILVER_RUNNER_SAFETY_MEMORY_LIMIT" in text
    )


def _ledger_status_for_error(exc: BaseException) -> str:
    if isinstance(exc, ControlledInterrupt):
        return "INTERRUPTED"
    if _is_clickhouse_session_locked(exc) or _is_python_memory_limit(exc):
        return "INTERRUPTED"
    if _is_clickhouse_memory_error(exc) or (
        isinstance(exc, SilverBuildError) and "STOP_SILVER_CH_MEMORY_LIMIT" in str(exc)
    ):
        return "INTERRUPTED"
    return "FAILED"


def _map_clickhouse_operational_error(exc: BaseException) -> SilverBuildError:
    if _is_clickhouse_memory_error(exc):
        return SilverBuildError(f"STOP_SILVER_CH_MEMORY_LIMIT: {exc}")
    if _is_clickhouse_session_locked(exc):
        return SilverBuildError(f"STOP_SILVER_CH_SESSION_LOCKED: {exc}")
    return SilverBuildError(f"STOP_SILVER_RUNNER_SAFETY_UNEXPECTED: {exc}")


def _bronze_payload_select(payload_mode: str) -> str:
    if payload_mode == BRONZE_PAYLOAD_EPOCH_PLAN:
        return (
            "multiIf("
            "e.message_type IN ('snapshot', 'checkpoint', 'gap_marker'), "
            "e.original_payload, "
            "''"
            ") AS original_payload"
        )
    if payload_mode == BRONZE_PAYLOAD_FULL:
        return "e.original_payload AS original_payload"
    raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_BRONZE_PAYLOAD_MODE_INVALID")


def iter_bronze_records(
    client: Any,
    config: BuildConfig,
    *,
    chain_index: int | None = None,
    start_apply_key: tuple[int, int] | None = None,
    end_apply_key: tuple[int, int] | None = None,
    payload_mode: str = BRONZE_PAYLOAD_FULL,
    message_types: Sequence[str] | None = None,
    event_time_ns_to: int | None = None,
) -> Iterator[BronzeRecord]:
    if chain_index is not None and (
        start_apply_key is not None or end_apply_key is not None
    ):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_APPLY_BOUNDS_INVALID")
    if (start_apply_key is None) != (end_apply_key is None):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_APPLY_BOUNDS_INVALID")
    if chain_index is not None:
        predicate = """
      AND e.canonical_segment_chain_index = {chain_index:UInt64}
        """
    elif start_apply_key is not None and end_apply_key is not None:
        predicate = """
      AND (e.canonical_segment_chain_index, e.record_ordinal) >=
          ({start_rank:UInt64}, {start_ordinal:UInt64})
      AND (e.canonical_segment_chain_index, e.record_ordinal) <
          ({end_rank:UInt64}, {end_ordinal:UInt64})
        """
    else:
        predicate = """
      AND e.canonical_segment_chain_index >= {start_index:UInt64}
      AND e.canonical_segment_chain_index <= {end_index:UInt64}
        """
    type_filter = ""
    if message_types is not None:
        if not message_types:
            raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_MESSAGE_TYPES_EMPTY")
        allowed = tuple(str(t).lower() for t in message_types)
        if any(t not in {"snapshot", "checkpoint", "delta", "gap_marker"} for t in allowed):
            raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_MESSAGE_TYPES_INVALID")
        type_filter = "AND e.message_type IN {message_types:Array(String)}"
    time_filter = ""
    if event_time_ns_to is not None:
        time_filter = "AND e.event_time_ns <= {event_time_ns_to:UInt64}"
    payload_select = _bronze_payload_select(payload_mode)
    sql = f"""
    SELECT
      e.record_id, e.symbol, e.message_type, e.event_time_ns, e.receive_time_ns,
      e.update_id, e.seq, e.update_id_present, e.seq_present,
      e.source_segment_sha256, e.record_ordinal, e.payload_sha256,
      {payload_select},
      e.canonical_segment_chain_index
    FROM {config.input_database}.{BRONZE_EVENTS_TABLE} AS e FINAL
    WHERE e.symbol = {{symbol:String}}
      AND e.chain_version = {{chain_version:String}}
      {predicate}
      {type_filter}
      {time_filter}
    ORDER BY e.canonical_segment_chain_index, e.record_ordinal
    """
    parameters: dict[str, Any] = {
        "symbol": config.symbol,
        "chain_version": config.chain_version,
        "start_index": config.start_chain_index,
        "end_index": config.end_chain_index,
    }
    if message_types is not None:
        parameters["message_types"] = [str(t).lower() for t in message_types]
    if event_time_ns_to is not None:
        parameters["event_time_ns_to"] = int(event_time_ns_to)
    if chain_index is not None:
        parameters["chain_index"] = int(chain_index)
    if start_apply_key is not None and end_apply_key is not None:
        parameters.update(
            {
                "start_rank": int(start_apply_key[0]),
                "start_ordinal": int(start_apply_key[1]),
                "end_rank": int(end_apply_key[0]),
                "end_ordinal": int(end_apply_key[1]),
            }
        )
    previous: tuple[int, int] | None = None
    try:
        with client.query_row_block_stream(
            sql,
            parameters=parameters,
            settings=BRONZE_STREAM_SETTINGS,
        ) as stream:
            for block in stream:
                for row in block:
                    record = bronze_row_from_ch(tuple(row))
                    key = _record_key(record)
                    if previous is not None and key <= previous:
                        raise SilverBuildError(
                            "STOP_SILVER_REPLAY_ORDER_MISMATCH: streamed order invalid"
                        )
                    previous = key
                    yield record
    except SilverBuildError:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_clickhouse_stream_error(exc)


def iter_bronze_epoch_plan_records(
    client: Any,
    config: BuildConfig,
) -> Iterator[BronzeRecord]:
    """Stream canonical Bronze rows for epoch planning without delta payloads."""
    for rank in range(config.start_chain_index, config.end_chain_index + 1):
        yield from iter_bronze_records(
            client,
            config,
            chain_index=rank,
            payload_mode=BRONZE_PAYLOAD_EPOCH_PLAN,
        )


def build_epoch_plan(
    client: Any,
    config: BuildConfig,
    metadata: dict[int, dict[str, Any]],
) -> BuildPlan:
    if not metadata:
        raise SilverBuildError("STOP_SILVER_EPOCH_PLAN_EMPTY")
    scan_end_ns = max(row["last_event_time_ns"] for row in metadata.values()) + 1
    clean_ranks = clean_segment_start_ranks(metadata)
    discovery = discover_epochs(
        iter_bronze_epoch_plan_records(client, config),
        chain_version=config.chain_version,
        canonical_chain_hash=config.expected_chain_hash,
        scan_end_ns=scan_end_ns,
        clean_segment_start_ranks=clean_ranks,
    )
    plan = BuildPlan(
        epochs=list(discovery.epochs),
        gaps=list(discovery.gaps),
        clean_segment_start_ranks=clean_ranks,
    )
    plan.finalize()
    return plan


def profile_epoch_plan(
    client: Any,
    config: BuildConfig,
    metadata: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Read-only epoch-plan build with streaming and memory accounting."""
    started = time.monotonic()
    rss_start = current_rss_bytes()
    metadata = metadata or load_segment_metadata(client, config)
    plan = build_epoch_plan(client, config, metadata)
    chunks = plan_epoch_chunks(
        plan.epochs,
        chunk_market_minutes=config.chunk_market_minutes,
        warmup_minutes=config.warmup_minutes,
    )
    from .silver_plan_coverage_parity_v1_3 import chunk_plan_hash

    rss_peak = current_rss_bytes()
    elapsed = time.monotonic() - started
    return {
        "status": "EPOCH_PLAN_PROFILED",
        "epoch_plan_hash": plan.epoch_plan_hash,
        "chunk_plan_hash": chunk_plan_hash(chunks),
        "epochs": len(plan.epochs),
        "gaps": len(plan.gaps),
        "chunks": len(chunks),
        "elapsed_s": round(elapsed, 3),
        "python_rss_start_bytes": rss_start,
        "python_rss_peak_bytes": rss_peak,
        "python_rss_peak_mib": round(rss_peak / (1024 * 1024), 3),
        "clickhouse_stream_settings": dict(BRONZE_STREAM_SETTINGS),
    }


def plan_epoch_chunks(
    epochs: Sequence[EpochDefinition],
    *,
    chunk_market_minutes: int,
    warmup_minutes: int,
) -> list[ChunkPlan]:
    if chunk_market_minutes <= 0:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_CHUNK_MINUTES_INVALID")
    chunk_ns = chunk_market_minutes * 60 * 1_000_000_000
    # Warm-up is a replay read prefix only. Full-book Silver output starts at
    # safe_start_ns because the exchange snapshot anchor already materializes
    # the book at the epoch boundary.
    replay_warmup_ns = warmup_minutes * 60 * 1_000_000_000
    chunks: list[ChunkPlan] = []
    for epoch_index, epoch in enumerate(epochs, 1):
        cursor = int(epoch.safe_start_ns)
        chunk_index = 0
        while cursor < int(epoch.safe_end_ns):
            analysis_end = min(cursor + chunk_ns, int(epoch.safe_end_ns))
            if analysis_end <= cursor:
                break
            decision = validate_epoch_window(
                [epoch],
                analysis_start_ns=cursor,
                analysis_end_ns=analysis_end,
                warmup_ns=0,
            )
            if decision.status != "OK":
                break
            chunk_index += 1
            chunk = ChunkPlan(
                epoch_index=epoch_index,
                chunk_index=chunk_index,
                epoch=epoch,
                analysis_start_ns=cursor,
                analysis_end_ns=analysis_end,
                warmup_ns=replay_warmup_ns,
            )
            chunk.materialize_ids()
            chunks.append(chunk)
            if analysis_end >= int(epoch.safe_end_ns):
                break
            cursor = analysis_end
    return chunks


def make_run_id(config: BuildConfig, epoch_plan_hash: str) -> str:
    return _hash(
        {
            "runner_version": RUNNER_VERSION,
            "build_schema_version": BUILD_SCHEMA_VERSION,
            "input_database": config.input_database,
            "output_database": config.output_database,
            "chain_version": config.chain_version,
            "canonical_chain_hash": config.expected_chain_hash,
            "epoch_plan_hash": epoch_plan_hash,
            "symbol": config.symbol,
            "chunk_market_minutes": config.chunk_market_minutes,
            "warmup_minutes": config.warmup_minutes,
        }
    )


@dataclass
class StopState:
    requested: bool = False
    signal_number: int = 0

    def request(self, signum: int) -> None:
        self.requested = True
        self.signal_number = int(signum)

    def check(self) -> None:
        if self.requested:
            raise ControlledInterrupt(
                f"STOP_SILVER_BUILD_INTERRUPTED_SIGNAL_{self.signal_number}"
            )


class BuildLock:
    def __init__(self, path: Path, metadata: dict[str, Any]):
        self.path = path
        self.metadata = metadata
        self.acquired = False
        self.token = hashlib.sha256(os.urandom(32)).hexdigest()

    @staticmethod
    def read(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SilverBuildError(
                f"STOP_SILVER_BUILD_STALE_LOCK_REQUIRES_MANUAL_REVIEW: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise SilverBuildError(
                "STOP_SILVER_BUILD_STALE_LOCK_REQUIRES_MANUAL_REVIEW"
            )
        return value

    @classmethod
    def inspect_existing(cls, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        metadata = cls.read(path)
        pid = int(metadata.get("pid") or 0)
        if pid > 0 and _pid_alive(pid):
            raise SilverBuildError("STOP_SILVER_BUILD_ALREADY_RUNNING")
        raise SilverBuildError(
            "STOP_SILVER_BUILD_STALE_LOCK_REQUIRES_MANUAL_REVIEW"
        )

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self.metadata, "token": self.token}
        try:
            descriptor = os.open(
                self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError:
            self.inspect_existing(self.path)
            raise AssertionError("unreachable")
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        self.acquired = True

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = self.read(self.path)
            if current.get("token") == self.token:
                self.path.unlink()
        finally:
            self.acquired = False

    def __enter__(self) -> "BuildLock":
        self.acquire()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.release()


def _chunk_status(
    client: Any, config: BuildConfig, chunk_key: str
) -> tuple[str, str, str, int, int, int] | None:
    rows = client.query(
        f"""
        SELECT
          status,
          epoch_hash,
          epoch_plan_hash,
          level_change_count,
          state_count,
          source_record_count
        FROM {config.output_database}.{CHUNKS_TABLE} FINAL
        WHERE chunk_key = {{chunk_key:String}}
        LIMIT 1
        """,
        parameters={"chunk_key": chunk_key},
    ).result_rows
    if not rows:
        return None
    return (
        _text(rows[0][0]),
        _text(rows[0][1]),
        _text(rows[0][2]),
        int(rows[0][3]),
        int(rows[0][4]),
        int(rows[0][5]),
    )


def _write_chunk_status(
    client: Any,
    config: BuildConfig,
    *,
    run_id: str,
    epoch_plan_hash: str,
    chunk: ChunkPlan,
    status: str,
    stop_reason: str = "",
    level_change_count: int = 0,
    state_count: int = 0,
    source_record_count: int = 0,
    output_hash: str = "0" * 64,
) -> None:
    prior = client.query(
        f"""
        SELECT version_ms
        FROM {config.output_database}.{CHUNKS_TABLE} FINAL
        WHERE chunk_key = {{chunk_key:String}}
        """,
        parameters={"chunk_key": chunk.chunk_key},
    ).result_rows
    version_ms = max(int(time.time() * 1000), int(prior[0][0]) + 1 if prior else 0)
    client.insert(
        f"{config.output_database}.{CHUNKS_TABLE}",
        [[
            chunk.chunk_key,
            chunk.build_id,
            run_id,
            chunk.epoch.epoch_id,
            chunk.epoch.epoch_hash,
            epoch_plan_hash,
            chunk.epoch.chain_version,
            chunk.epoch.canonical_chain_hash,
            chunk.epoch.symbol,
            chunk.analysis_start_ns,
            chunk.analysis_end_ns,
            chunk.warmup_ns,
            status,
            stop_reason[:4096],
            level_change_count,
            state_count,
            source_record_count,
            output_hash,
            version_ms,
        ]],
        column_names=[
            "chunk_key",
            "build_id",
            "run_id",
            "epoch_id",
            "epoch_hash",
            "epoch_plan_hash",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "chunk_start_ns",
            "chunk_end_ns",
            "warmup_ns",
            "status",
            "stop_reason",
            "level_change_count",
            "state_count",
            "source_record_count",
            "output_hash",
            "version_ms",
        ],
    )


def persist_epochs_and_gaps(
    client: Any,
    config: BuildConfig,
    plan: BuildPlan,
) -> None:
    existing = client.query(
        f"""
        SELECT epoch_id, epoch_hash
        FROM {config.output_database}.{EPOCHS_TABLE} FINAL
        WHERE chain_version = {{chain_version:String}}
        """,
        parameters={"chain_version": config.chain_version},
    ).result_rows
    existing_epochs = {_text(row[0]): _text(row[1]) for row in existing}
    existing_gap_rows = client.query(
        f"""
        SELECT gap_id
        FROM {config.output_database}.{GAPS_TABLE} FINAL
        WHERE chain_version = {{chain_version:String}}
        """,
        parameters={"chain_version": config.chain_version},
    ).result_rows
    existing_gaps = {_text(row[0]) for row in existing_gap_rows}
    now = int(time.time() * 1000)
    epoch_rows = []
    for epoch in plan.epochs:
        prior = existing_epochs.get(epoch.epoch_id)
        if prior is not None and prior != epoch.epoch_hash:
            raise SilverBuildError("STOP_SILVER_EPOCH_PLAN_CHANGED: epoch hash mismatch")
        if prior is None:
            epoch_rows.append([
                epoch.epoch_id,
                epoch.epoch_hash,
                epoch.chain_version,
                epoch.canonical_chain_hash,
                epoch.symbol,
                epoch.anchor_type,
                epoch.anchor_provenance,
                epoch.anchor_event_time_ns,
                epoch.anchor_receive_time_ns,
                epoch.anchor_u,
                epoch.anchor_seq,
                epoch.anchor_segment_chain_index,
                epoch.anchor_record_ordinal,
                epoch.safe_start_ns,
                epoch.safe_end_ns,
                epoch.terminating_reason,
                epoch.preceding_gap_id,
                epoch.status,
                int(epoch.reconnect_resync_proven),
                now,
            ])
    if epoch_rows:
        client.insert(
            f"{config.output_database}.{EPOCHS_TABLE}",
            epoch_rows,
            column_names=[
                "epoch_id",
                "epoch_hash",
                "chain_version",
                "canonical_chain_hash",
                "symbol",
                "anchor_type",
                "anchor_provenance",
                "anchor_event_time_ns",
                "anchor_receive_time_ns",
                "anchor_u",
                "anchor_seq",
                "anchor_segment_chain_index",
                "anchor_record_ordinal",
                "safe_start_ns",
                "safe_end_ns",
                "terminating_reason",
                "preceding_gap_id",
                "status",
                "reconnect_resync_proven",
                "version_ms",
            ],
        )
    gap_rows = []
    for gap in plan.gaps:
        if gap.gap_id in existing_gaps:
            continue
        gap_rows.append([
            gap.gap_id,
            config.chain_version,
            config.expected_chain_hash,
            config.symbol,
            gap.reason,
            gap.segment_chain_index,
            gap.record_ordinal,
            gap.gap_time_ns,
            now,
        ])
    if gap_rows:
        client.insert(
            f"{config.output_database}.{GAPS_TABLE}",
            gap_rows,
            column_names=[
                "gap_id",
                "chain_version",
                "canonical_chain_hash",
                "symbol",
                "reason",
                "segment_chain_index",
                "record_ordinal",
                "gap_time_ns",
                "version_ms",
            ],
        )


def _flush_insert_rows(
    client: Any,
    *,
    table: str,
    column_names: Sequence[str],
    columns: list[list[Any]],
) -> None:
    """Native column-oriented ClickHouse insert; no row-by-row round trips."""
    if not columns or not columns[0]:
        return
    client.insert(
        table,
        columns,
        column_names=list(column_names),
        column_oriented=True,
    )
    _check_rss()


class _StreamingLevelChangeSink:
    """Byte-/row-capped LC insert sink used during replay to bound Python RSS."""

    def __init__(
        self,
        client: Any,
        config: BuildConfig,
        *,
        chunk: ChunkPlan,
        version_ms: int,
        batch_size: int,
        batch_max_bytes: int,
    ) -> None:
        self._client = client
        self._config = config
        self._chunk = chunk
        self._version_ms = version_ms
        self._batch_size = batch_size
        self._batch_max_bytes = batch_max_bytes
        self._columns = [
            "row_id",
            "build_id",
            "chunk_key",
            "epoch_id",
            "epoch_hash",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "canonical_segment_chain_index",
            "source_segment_sha256",
            "record_ordinal",
            "apply_order",
            "event_time_ns",
            "receive_time_ns",
            "payload",
            "version_ms",
        ]
        self._buffers: list[list[Any]] = [[] for _ in self._columns]
        self._bytes = 0
        self.insert_calls = 0
        self.rows = 0

    def __call__(self, row: dict[str, Any]) -> None:
        payload = json.dumps(row, separators=(",", ":"))
        payload_len = len(payload)
        if self._buffers[0] and (
            len(self._buffers[0]) >= self._batch_size
            or self._bytes + payload_len > self._batch_max_bytes
        ):
            self.flush()
        row_id = level_change_row_id(
            chunk_key=self._chunk.chunk_key,
            segment_rank=int(row["canonical_segment_chain_index"]),
            record_ordinal=int(row["source_record_ordinal"]),
            apply_order=int(row["apply_order"]),
        )
        values = [
            row_id,
            self._chunk.build_id,
            self._chunk.chunk_key,
            self._chunk.epoch.epoch_id,
            self._chunk.epoch.epoch_hash,
            self._chunk.epoch.chain_version,
            self._chunk.epoch.canonical_chain_hash,
            self._chunk.epoch.symbol,
            row["canonical_segment_chain_index"],
            row.get("source_segment_sha256", "0" * 64),
            row["source_record_ordinal"],
            row["apply_order"],
            row["event_time_ns"],
            row.get("receive_time_ns", row["event_time_ns"]),
            payload,
            self._version_ms,
        ]
        for buf, value in zip(self._buffers, values):
            buf.append(value)
        self._bytes += payload_len
        self.rows += 1

    def flush(self) -> None:
        if not self._buffers[0]:
            return
        _flush_insert_rows(
            self._client,
            table=f"{self._config.output_database}.{LEVEL_CHANGES_TABLE}",
            column_names=self._columns,
            columns=self._buffers,
        )
        self.insert_calls += 1
        for buf in self._buffers:
            buf.clear()
        self._bytes = 0


class _StreamingStateSink:
    """Byte-/row-capped 100ms-state insert sink used during replay."""

    def __init__(
        self,
        client: Any,
        config: BuildConfig,
        *,
        chunk: ChunkPlan,
        version_ms: int,
        batch_size: int,
        batch_max_bytes: int,
    ) -> None:
        self._client = client
        self._config = config
        self._chunk = chunk
        self._version_ms = version_ms
        self._batch_size = batch_size
        self._batch_max_bytes = batch_max_bytes
        self._columns = [
            "row_id",
            "build_id",
            "chunk_key",
            "epoch_id",
            "epoch_hash",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "bucket_start_ns",
            "payload",
            "version_ms",
        ]
        self._buffers: list[list[Any]] = [[] for _ in self._columns]
        self._bytes = 0
        self.insert_calls = 0
        self.rows = 0

    def __call__(self, row: dict[str, Any]) -> None:
        bucket_ns = int(row["bucket_start_ms"]) * 1_000_000
        payload = json.dumps(row, separators=(",", ":"))
        payload_len = len(payload)
        if self._buffers[0] and (
            len(self._buffers[0]) >= self._batch_size
            or self._bytes + payload_len > self._batch_max_bytes
        ):
            self.flush()
        values = [
            state_row_id(chunk_key=self._chunk.chunk_key, bucket_start_ns=bucket_ns),
            self._chunk.build_id,
            self._chunk.chunk_key,
            self._chunk.epoch.epoch_id,
            self._chunk.epoch.epoch_hash,
            self._chunk.epoch.chain_version,
            self._chunk.epoch.canonical_chain_hash,
            self._chunk.epoch.symbol,
            bucket_ns,
            payload,
            self._version_ms,
        ]
        for buf, value in zip(self._buffers, values):
            buf.append(value)
        self._bytes += payload_len
        self.rows += 1

    def flush(self) -> None:
        if not self._buffers[0]:
            return
        _flush_insert_rows(
            self._client,
            table=f"{self._config.output_database}.{METRICS_TABLE}",
            column_names=self._columns,
            columns=self._buffers,
        )
        self.insert_calls += 1
        for buf in self._buffers:
            buf.clear()
        self._bytes = 0


def _persist_chunk_outputs(
    client: Any,
    config: BuildConfig,
    *,
    chunk: ChunkPlan,
    replay: ReplayResult,
    batch_size: int | None = None,
    batch_max_bytes: int | None = None,
    level_changes_already_persisted: bool = False,
    states_already_persisted: bool = False,
    lc_insert_calls: int = 0,
    state_insert_calls: int = 0,
    version_ms: int | None = None,
) -> dict[str, Any]:
    """Persist LC + 100ms states in byte-capped batches; return output hash + stats."""
    batch_size = int(batch_size or INSERT_BATCH_SIZE)
    batch_max_bytes = int(batch_max_bytes or INSERT_BATCH_MAX_BYTES)
    if batch_size <= 0:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_INSERT_BATCH_INVALID")
    version_ms = int(version_ms if version_ms is not None else time.time() * 1000)
    lc_insert_calls = int(lc_insert_calls)
    st_insert_calls = int(state_insert_calls)
    level_change_count = int(
        getattr(replay, "level_change_count", None)
        if getattr(replay, "level_change_count", None) is not None
        else len(replay.level_changes)
    )
    state_count = int(
        getattr(replay, "state_count", None)
        if getattr(replay, "state_count", None) is not None
        else len(replay.states)
    )
    expected_states = analysis_bucket_count(
        chunk.analysis_start_ns, chunk.analysis_end_ns
    )
    if state_count != expected_states:
        raise SilverBuildError(
            "STOP_SILVER_BUCKET_COUNT_MISMATCH: "
            f"states={state_count} expected={expected_states}"
        )

    if not level_changes_already_persisted:
        lc_columns = [
            "row_id",
            "build_id",
            "chunk_key",
            "epoch_id",
            "epoch_hash",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "canonical_segment_chain_index",
            "source_segment_sha256",
            "record_ordinal",
            "apply_order",
            "event_time_ns",
            "receive_time_ns",
            "payload",
            "version_ms",
        ]
        lc_buffers: list[list[Any]] = [[] for _ in lc_columns]
        lc_bytes = 0
        lc_table = f"{config.output_database}.{LEVEL_CHANGES_TABLE}"

        def flush_lc() -> None:
            nonlocal lc_bytes, lc_insert_calls
            if not lc_buffers[0]:
                return
            _flush_insert_rows(
                client, table=lc_table, column_names=lc_columns, columns=lc_buffers
            )
            lc_insert_calls += 1
            for buf in lc_buffers:
                buf.clear()
            lc_bytes = 0

        for row in replay.level_changes:
            payload = json.dumps(row, separators=(",", ":"))
            payload_len = len(payload)
            if lc_buffers[0] and (
                len(lc_buffers[0]) >= batch_size or lc_bytes + payload_len > batch_max_bytes
            ):
                flush_lc()
            row_id = level_change_row_id(
                chunk_key=chunk.chunk_key,
                segment_rank=int(row["canonical_segment_chain_index"]),
                record_ordinal=int(row["source_record_ordinal"]),
                apply_order=int(row["apply_order"]),
            )
            values = [
                row_id,
                chunk.build_id,
                chunk.chunk_key,
                chunk.epoch.epoch_id,
                chunk.epoch.epoch_hash,
                chunk.epoch.chain_version,
                chunk.epoch.canonical_chain_hash,
                chunk.epoch.symbol,
                row["canonical_segment_chain_index"],
                row.get("source_segment_sha256", "0" * 64),
                row["source_record_ordinal"],
                row["apply_order"],
                row["event_time_ns"],
                row.get("receive_time_ns", row["event_time_ns"]),
                payload,
                version_ms,
            ]
            for buf, value in zip(lc_buffers, values):
                buf.append(value)
            lc_bytes += payload_len
        flush_lc()

    if not states_already_persisted:
        state_columns = [
            "row_id",
            "build_id",
            "chunk_key",
            "epoch_id",
            "epoch_hash",
            "chain_version",
            "canonical_chain_hash",
            "symbol",
            "bucket_start_ns",
            "payload",
            "version_ms",
        ]
        st_buffers: list[list[Any]] = [[] for _ in state_columns]
        st_bytes = 0
        st_table = f"{config.output_database}.{METRICS_TABLE}"

        def flush_states() -> None:
            nonlocal st_bytes, st_insert_calls
            if not st_buffers[0]:
                return
            _flush_insert_rows(
                client, table=st_table, column_names=state_columns, columns=st_buffers
            )
            st_insert_calls += 1
            for buf in st_buffers:
                buf.clear()
            st_bytes = 0

        for row in replay.states:
            bucket_ns = int(row["bucket_start_ms"]) * 1_000_000
            payload = json.dumps(row, separators=(",", ":"))
            payload_len = len(payload)
            if st_buffers[0] and (
                len(st_buffers[0]) >= batch_size or st_bytes + payload_len > batch_max_bytes
            ):
                flush_states()
            values = [
                state_row_id(chunk_key=chunk.chunk_key, bucket_start_ns=bucket_ns),
                chunk.build_id,
                chunk.chunk_key,
                chunk.epoch.epoch_id,
                chunk.epoch.epoch_hash,
                chunk.epoch.chain_version,
                chunk.epoch.canonical_chain_hash,
                chunk.epoch.symbol,
                bucket_ns,
                payload,
                version_ms,
            ]
            for buf, value in zip(st_buffers, values):
                buf.append(value)
            st_bytes += payload_len
        flush_states()

    output_hash = _hash(
        {
            "level_change_apply_hash": replay.level_change_hash_apply_order,
            "level_change_count": level_change_count,
            "state_count": state_count,
            "last_apply_key": replay.end_apply_key,
        }
    )
    return {
        "output_hash": output_hash,
        "insert_calls_level_changes": lc_insert_calls,
        "insert_calls_states": st_insert_calls,
        "batch_size": batch_size,
        "batch_max_bytes": batch_max_bytes,
        "level_change_count": level_change_count,
        "state_count": state_count,
        "version_ms": version_ms,
    }


def _verify_chunk_inserts(
    client: Any,
    config: BuildConfig,
    *,
    chunk: ChunkPlan,
    level_change_count: int,
    state_count: int,
) -> None:
    lc = int(
        client.query(
            f"""
            SELECT count()
            FROM {config.output_database}.{LEVEL_CHANGES_TABLE} FINAL
            WHERE chunk_key = {{chunk_key:String}}
            """,
            parameters={"chunk_key": chunk.chunk_key},
        ).result_rows[0][0]
    )
    st = int(
        client.query(
            f"""
            SELECT count()
            FROM {config.output_database}.{METRICS_TABLE} FINAL
            WHERE chunk_key = {{chunk_key:String}}
            """,
            parameters={"chunk_key": chunk.chunk_key},
        ).result_rows[0][0]
    )
    if lc != level_change_count or st != state_count:
        raise SilverBuildError(
            "STOP_SILVER_VERIFY_OUTPUT_MISMATCH: "
            f"lc={lc}/{level_change_count} states={st}/{state_count}"
        )


def _chunk_market_minutes(chunk: ChunkPlan) -> float:
    return max(
        (chunk.analysis_end_ns - chunk.analysis_start_ns) / 60_000_000_000,
        1e-9,
    )


def build_one_chunk(
    client: Any,
    config: BuildConfig,
    *,
    run_id: str,
    epoch_plan_hash: str,
    chunk: ChunkPlan,
    stop: StopState,
) -> dict[str, Any]:
    clients = _as_session_clients(client)
    existing = _chunk_status(clients.write, config, chunk.chunk_key)
    if existing is not None:
        (
            status,
            epoch_hash,
            stored_plan_hash,
            lc_count,
            st_count,
            src_count,
        ) = existing
        if epoch_hash != chunk.epoch.epoch_hash or stored_plan_hash != epoch_plan_hash:
            raise SilverBuildError("STOP_SILVER_EPOCH_PLAN_CHANGED: chunk contract drift")
        if status == "COMPLETE":
            return {
                "status": "SKIPPED_ALREADY_COMPLETE",
                "chunk_key": chunk.chunk_key,
                "rows_inserted": 0,
                "level_change_count": lc_count,
                "state_count": st_count,
                "source_record_count": src_count,
            }
        if status in {"RUNNING", "INTERRUPTED", "FAILED"} and not config.resume:
            raise SilverBuildError("STOP_SILVER_RESUME_INCOMPLETE_REQUIRES_RESUME")

    _write_chunk_status(
        clients.write,
        config,
        run_id=run_id,
        epoch_plan_hash=epoch_plan_hash,
        chunk=chunk,
        status="RUNNING",
    )
    bronze_stream: Any | None = None
    replay: ReplayResult | None = None
    lc_sink: _StreamingLevelChangeSink | None = None
    state_sink: _StreamingStateSink | None = None
    version_ms = int(time.time() * 1000)
    try:
        start_apply, end_apply = epoch_apply_bounds(chunk.epoch)
        try:
            bronze_stream = iter_bronze_records(
                clients.read,
                config,
                start_apply_key=start_apply,
                end_apply_key=end_apply,
                payload_mode=BRONZE_PAYLOAD_FULL,
            )
            try:
                resume_record, replay_stream = split_resume_and_replay_stream(
                    bronze_stream,
                    epoch=chunk.epoch,
                    analysis_start_ns=chunk.analysis_start_ns,
                )
            except EpochSilverError as exc:
                raise SilverBuildError(str(exc)) from exc
            stop.check()
            lc_sink = _StreamingLevelChangeSink(
                clients.write,
                config,
                chunk=chunk,
                version_ms=version_ms,
                batch_size=INSERT_BATCH_SIZE,
                batch_max_bytes=INSERT_BATCH_MAX_BYTES,
            )
            state_sink = _StreamingStateSink(
                clients.write,
                config,
                chunk=chunk,
                version_ms=version_ms,
                batch_size=INSERT_BATCH_SIZE,
                batch_max_bytes=INSERT_BATCH_MAX_BYTES,
            )
            try:
                replay = replay_epoch_window(
                    replay_stream,
                    epoch=chunk.epoch,
                    resume_record=resume_record,
                    analysis_start_ns=chunk.analysis_start_ns,
                    analysis_end_ns=chunk.analysis_end_ns,
                    build_id=chunk.build_id,
                    level_change_sink=lc_sink,
                    retain_level_changes=False,
                    state_sink=state_sink,
                    retain_states=False,
                )
            except EpochSilverError as exc:
                raise SilverBuildError(str(exc)) from exc
        finally:
            # Replay may break early at analysis_end_ns while the Bronze HTTP
            # stream is still open. Close it before any write/verify query on
            # a shared legacy client, and always before returning.
            if bronze_stream is not None:
                _close_streaming_iterator(bronze_stream)
                bronze_stream = None
        if replay is None or lc_sink is None or state_sink is None:
            raise SilverBuildError("STOP_SILVER_REPLAY_INCOMPLETE")
        stop.check()
        persist_stats = _persist_chunk_outputs(
            clients.write,
            config,
            chunk=chunk,
            replay=replay,
            level_changes_already_persisted=True,
            states_already_persisted=True,
            lc_insert_calls=lc_sink.insert_calls,
            state_insert_calls=state_sink.insert_calls,
            version_ms=version_ms,
        )
        level_change_count = int(persist_stats["level_change_count"])
        state_count = int(persist_stats["state_count"])
        _verify_chunk_inserts(
            clients.verify,
            config,
            chunk=chunk,
            level_change_count=level_change_count,
            state_count=state_count,
        )
        _write_chunk_status(
            clients.write,
            config,
            run_id=run_id,
            epoch_plan_hash=epoch_plan_hash,
            chunk=chunk,
            status="COMPLETE",
            level_change_count=level_change_count,
            state_count=state_count,
            source_record_count=replay.source_records,
            output_hash=persist_stats["output_hash"],
        )
        return {
            "status": "COMPLETE",
            "chunk_key": chunk.chunk_key,
            "rows_inserted": level_change_count + state_count,
            "level_change_count": level_change_count,
            "state_count": state_count,
            "source_record_count": replay.source_records,
            "output_hash": persist_stats["output_hash"],
            "insert_calls_level_changes": persist_stats["insert_calls_level_changes"],
            "insert_calls_states": persist_stats["insert_calls_states"],
            "batch_size": persist_stats["batch_size"],
            "session_ids": clients.session_ids(),
            "evaluation_end_ns": replay.evaluation_end_ns,
        }
    except ControlledInterrupt as exc:
        # Newer append-only ledger version; resume rebuilds RUNNING/INTERRUPTED/FAILED.
        _write_chunk_status(
            clients.write,
            config,
            run_id=run_id,
            epoch_plan_hash=epoch_plan_hash,
            chunk=chunk,
            status="INTERRUPTED",
            stop_reason=str(exc),
        )
        raise
    except SilverBuildError as exc:
        ledger_status = _ledger_status_for_error(exc)
        _write_chunk_status(
            clients.write,
            config,
            run_id=run_id,
            epoch_plan_hash=epoch_plan_hash,
            chunk=chunk,
            status=ledger_status,
            stop_reason=str(exc),
        )
        raise
    except Exception as exc:  # noqa: BLE001
        mapped = _map_clickhouse_operational_error(exc)
        ledger_status = _ledger_status_for_error(mapped)
        try:
            _write_chunk_status(
                clients.write,
                config,
                run_id=run_id,
                epoch_plan_hash=epoch_plan_hash,
                chunk=chunk,
                status=ledger_status,
                stop_reason=str(mapped),
            )
        except Exception:  # noqa: BLE001
            pass
        raise mapped from exc
    finally:
        if bronze_stream is not None:
            _close_streaming_iterator(bronze_stream)


def run_build(
    client: Any,
    config: BuildConfig,
    plan: BuildPlan,
    *,
    stop: StopState | None = None,
) -> dict[str, Any]:
    stop = stop or StopState()
    clients = _as_session_clients(client)
    if not clients.shared_legacy:
        clients.assert_isolated()
    run_id = make_run_id(config, plan.epoch_plan_hash)
    plan.chunks = plan_epoch_chunks(
        plan.epochs,
        chunk_market_minutes=config.chunk_market_minutes,
        warmup_minutes=config.warmup_minutes,
    )
    if not plan.chunks:
        raise SilverBuildError("STOP_SILVER_EPOCH_PLAN_EMPTY: no safe chunks")

    existing_run = clients.write.query(
        f"""
        SELECT epoch_plan_hash, status
        FROM {config.output_database}.{RUNS_TABLE} FINAL
        WHERE run_id = {{run_id:String}}
        """,
        parameters={"run_id": run_id},
    ).result_rows
    if existing_run:
        stored_hash = _text(existing_run[0][0])
        if stored_hash != plan.epoch_plan_hash:
            raise SilverBuildError("STOP_SILVER_EPOCH_PLAN_CHANGED: run hash mismatch")

    persist_epochs_and_gaps(clients.write, config, plan)
    started = time.monotonic()
    wall_started = datetime.now(timezone.utc)
    completed = skipped = 0
    total_rows = 0
    cumulative_level_changes = 0
    cumulative_states = 0
    completed_market_minutes = 0.0
    progress: list[dict[str, Any]] = []
    session_ids = clients.session_ids()
    for position, chunk in enumerate(plan.chunks, 1):
        stop.check()
        _check_resources(clients.verify, config)
        chunk_started = time.monotonic()
        result = build_one_chunk(
            clients,
            config,
            run_id=run_id,
            epoch_plan_hash=plan.epoch_plan_hash,
            chunk=chunk,
            stop=stop,
        )
        chunk_elapsed = max(time.monotonic() - chunk_started, 1e-9)
        status = result["status"]
        skipped += int(status == "SKIPPED_ALREADY_COMPLETE")
        completed += int(status == "COMPLETE")
        level_changes = int(result.get("level_change_count", 0))
        states_100ms = int(result.get("state_count", 0))
        total_rows += int(result.get("rows_inserted", 0))
        if status in {"COMPLETE", "SKIPPED_ALREADY_COMPLETE"}:
            # Skip path returns verified ledger counts; COMPLETE returns fresh counts.
            cumulative_level_changes += level_changes
            cumulative_states += states_100ms
        market_minutes = _chunk_market_minutes(chunk)
        if status in {"COMPLETE", "SKIPPED_ALREADY_COMPLETE"}:
            completed_market_minutes += market_minutes
        total_elapsed = max(time.monotonic() - started, 1e-9)
        chunks_done = completed + skipped
        remaining_chunks = len(plan.chunks) - position
        remaining_market_minutes = sum(
            _chunk_market_minutes(remaining) for remaining in plan.chunks[position:]
        )
        cumulative_spm = (
            total_elapsed / completed_market_minutes
            if completed_market_minutes > 0
            else 0.0
        )
        eta_seconds = (
            remaining_market_minutes * cumulative_spm
            if completed_market_minutes > 0
            else None
        )
        eta_utc = None
        if eta_seconds is not None:
            eta_utc = (
                wall_started + timedelta(seconds=float(eta_seconds))
            ).isoformat().replace("+00:00", "Z")
        row = {
            "chunk": f"{position}/{len(plan.chunks)}",
            "epoch": f"{chunk.epoch_index}/{len(plan.epochs)}",
            "segment_rank": chunk.epoch.anchor_segment_chain_index,
            "market_window": f"{_ns_iso(chunk.analysis_start_ns)}..{_ns_iso(chunk.analysis_end_ns)}",
            "level_changes": level_changes,
            "states_100ms": states_100ms,
            "cumulative_level_changes": cumulative_level_changes,
            "cumulative_states_100ms": cumulative_states,
            "chunk_elapsed_s": round(chunk_elapsed, 3),
            "total_elapsed_s": round(total_elapsed, 3),
            "chunk_seconds_per_market_minute": round(chunk_elapsed / market_minutes, 3),
            "cumulative_seconds_per_market_minute": round(cumulative_spm, 3),
            "completed_market_minutes": round(completed_market_minutes, 6),
            "chunks_complete": chunks_done,
            "chunks_total": len(plan.chunks),
            "remaining_chunks": remaining_chunks,
            "eta_s": None if eta_seconds is None else round(eta_seconds, 3),
            "eta_seconds": None if eta_seconds is None else round(eta_seconds, 3),
            "eta_utc": eta_utc,
            "peak_rss_mib": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3
            ),
            "status": status,
            "session_ids": result.get("session_ids") or session_ids,
            "insert_calls_level_changes": result.get("insert_calls_level_changes"),
            "insert_calls_states": result.get("insert_calls_states"),
        }
        progress.append(row)
        print(json.dumps(row, sort_keys=True), flush=True)
        if position % config.progress_every_chunks == 0 or position == len(plan.chunks):
            _write_report(
                config.report_path,
                {
                    "status": "RUNNING",
                    "run_id": run_id,
                    "epoch_plan_hash": plan.epoch_plan_hash,
                    "chunks_processed": position,
                    "chunks_total": len(plan.chunks),
                    "last_progress": row,
                    "session_ids": session_ids,
                },
            )

    build_hash = _hash(
        {
            "run_id": run_id,
            "epoch_plan_hash": plan.epoch_plan_hash,
            "chunk_keys": [chunk.chunk_key for chunk in plan.chunks],
            "progress": progress,
        }
    )
    clients.write.insert(
        f"{config.output_database}.{RUNS_TABLE}",
        [[
            run_id,
            RUNNER_VERSION,
            BUILD_SCHEMA_VERSION,
            config.chain_version,
            config.expected_chain_hash,
            plan.epoch_plan_hash,
            config.symbol,
            config.input_database,
            config.output_database,
            "COMPLETE",
            len(plan.chunks),
            completed + skipped,
            build_hash,
            socket.gethostname(),
            os.getpid(),
            int(time.time() * 1000),
        ]],
        column_names=[
            "run_id",
            "runner_version",
            "build_schema_version",
            "chain_version",
            "canonical_chain_hash",
            "epoch_plan_hash",
            "symbol",
            "input_database",
            "output_database",
            "status",
            "chunk_total",
            "chunk_complete",
            "build_hash",
            "host",
            "pid",
            "version_ms",
        ],
    )
    return {
        "status": "COMPLETE",
        "run_id": run_id,
        "epoch_plan_hash": plan.epoch_plan_hash,
        "build_hash": build_hash,
        "epochs": len(plan.epochs),
        "gaps": len(plan.gaps),
        "chunks": len(plan.chunks),
        "completed_chunks": completed,
        "skipped_chunks": skipped,
        "rows_inserted": total_rows,
        "elapsed_s": round(time.monotonic() - started, 3),
        "progress": progress,
        "session_ids": session_ids,
    }


def verify_build(client: Any, config: BuildConfig) -> dict[str, Any]:
    if not target_schema_exists(client, config.output_database):
        raise SilverBuildError("STOP_SILVER_VERIFY_SCHEMA_MISSING")
    bad_chunks = int(
        client.query(
            f"""
            SELECT count()
            FROM {config.output_database}.{CHUNKS_TABLE} FINAL
            WHERE status IN ('RUNNING', 'INTERRUPTED', 'FAILED')
            """,
        ).result_rows[0][0]
    )
    if bad_chunks:
        raise SilverBuildError("STOP_SILVER_VERIFY_INCOMPLETE_CHUNKS")
    dup_lc = int(
        client.query(
            f"""
            SELECT count() - uniqExact(row_id)
            FROM {config.output_database}.{LEVEL_CHANGES_TABLE} FINAL
            """,
        ).result_rows[0][0]
    )
    dup_metrics = int(
        client.query(
            f"""
            SELECT count() - uniqExact(row_id)
            FROM {config.output_database}.{METRICS_TABLE} FINAL
            """,
        ).result_rows[0][0]
    )
    utc_bad = int(
        client.query(
            f"""
            SELECT count()
            FROM {config.output_database}.{LEVEL_CHANGES_TABLE} FINAL
            WHERE toUnixTimestamp64Nano(event_time) != event_time_ns
               OR toUnixTimestamp64Nano(receive_time) != receive_time_ns
            """,
        ).result_rows[0][0]
    )
    if dup_lc or dup_metrics or utc_bad:
        raise SilverBuildError("STOP_SILVER_VERIFY_OUTPUT_MISMATCH")
    return {
        "status": "VERIFIED",
        "duplicate_level_changes": dup_lc,
        "duplicate_metrics": dup_metrics,
        "utc_ns_mismatches": utc_bad,
    }


def preflight(
    client: Any,
    config: BuildConfig,
    *,
    require_output_schema: bool = False,
    check_lock: bool = True,
    bronze_probe: dict[str, Any] | None = None,
    require_bronze_ready: bool = True,
    enforce_bucket_repair_gate: bool = False,
) -> dict[str, Any]:
    validate_input_database(config.input_database)
    validate_output_database(config.output_database)
    identity = code_identity()
    if identity["branch"] != EXPECTED_BRANCH or identity["head"] == "UNKNOWN":
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_WRONG_BRANCH")
    if config.start_chain_index < 0 or config.end_chain_index < config.start_chain_index:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_CHAIN_RANGE_INVALID")
    if config.end_chain_index >= config.expected_segment_count:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_CHAIN_RANGE_INVALID")
    if config.progress_every_chunks < 1:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_PROGRESS_INTERVAL_INVALID")
    if (
        config.enforce_canonical_lock_path
        and config.lock_path.resolve() != DEFAULT_LOCK_PATH.resolve()
    ):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_NONCANONICAL_LOCK_PATH")
    client.command("SELECT 1")
    client.command("SET max_threads = 1")
    rss, free, available = _check_resources(client, config)
    bronze = (
        bronze_hard_preflight(client, config, bronze_probe=bronze_probe)
        if require_bronze_ready
        else {"status": "SKIPPED_BRONZE_NOT_REQUIRED"}
    )
    schema_exists = target_schema_exists(client, config.output_database)
    if require_output_schema and not schema_exists:
        raise SilverBuildError("STOP_SILVER_VERIFY_SCHEMA_MISSING")
    if check_lock:
        BuildLock.inspect_existing(config.lock_path)
        if config.output_database == DEFAULT_OUTPUT_DATABASE:
            BuildLock.inspect_existing(DEFAULT_REPAIR_LOCK_PATH)
    repair_gate = None
    if (
        enforce_bucket_repair_gate
        and config.output_database == DEFAULT_OUTPUT_DATABASE
        and schema_exists
    ):
        from .silver_bucket_boundary_repair_v1_3 import (
            assert_production_bucket_repair_verified,
        )

        assert_production_bucket_repair_verified(
            client,
            chain_version=config.chain_version,
            output_database=config.output_database,
        )
        repair_gate = "VERIFIED_OR_NO_COMPLETE_HOLES"
    return {
        "status": "PASS",
        "mode": "READ_ONLY_PREFLIGHT",
        "code": identity,
        "input_database": config.input_database,
        "output_database": config.output_database,
        "target_schema_exists": schema_exists,
        "rss_bytes": rss,
        "clickhouse_free_bytes": free,
        "available_memory_bytes": available,
        "bronze": bronze,
        "lock_status": "FREE",
        "bucket_repair_gate": repair_gate,
    }


def _install_signal_handlers(stop: StopState) -> dict[int, Any]:
    previous: dict[int, Any] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda received, _frame, s=stop: s.request(received))
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _lock_metadata(config: BuildConfig, epoch_plan_hash: str = "") -> dict[str, Any]:
    identity = code_identity()
    return {
        "pid": os.getpid(),
        "started_at": _now_iso(),
        "host": socket.gethostname(),
        "input_database": config.input_database,
        "output_database": config.output_database,
        "chain_version": config.chain_version,
        "chain_hash": config.expected_chain_hash,
        "epoch_plan_hash": epoch_plan_hash,
        "symbol": config.symbol,
        "branch": identity["branch"],
        "head": identity["head"],
        "runner_version": RUNNER_VERSION,
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guarded BTC Silver v1.3 full-build runner"
    )
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--input-database", default=DEFAULT_INPUT_DATABASE)
    parser.add_argument("--output-database", default=DEFAULT_OUTPUT_DATABASE)
    parser.add_argument("--chain-version", required=True)
    parser.add_argument("--expected-chain-hash", required=True)
    parser.add_argument("--expected-bronze-records", type=int, default=EXPECTED_BRONZE_RECORDS)
    parser.add_argument("--start-chain-index", type=int, default=0)
    parser.add_argument("--end-chain-index", type=int, default=163)
    parser.add_argument(
        "--chunk-market-minutes",
        type=int,
        default=DEFAULT_CHUNK_MARKET_MINUTES,
    )
    parser.add_argument("--warmup-minutes", type=int, default=DEFAULT_WARMUP_MINUTES)
    parser.add_argument("--init-schema", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument(
        "--epoch-plan-only",
        action="store_true",
        help="Read-only streaming epoch-plan build with memory accounting.",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--max-rss-mib", type=int, default=DEFAULT_MAX_RSS_MIB)
    parser.add_argument(
        "--min-free-disk-gib", type=float, default=DEFAULT_MIN_FREE_DISK_GIB
    )
    parser.add_argument(
        "--min-available-memory-mib",
        type=int,
        default=DEFAULT_MIN_AVAILABLE_MEMORY_MIB,
    )
    parser.add_argument("--progress-every-chunks", type=int, default=1)
    parser.add_argument("--report-path", type=Path, required=True)
    parser.add_argument("--lock-path", type=Path, required=True)
    parser.add_argument(
        "--allow-noncanonical-lock-path",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser


def config_from_args(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(
        symbol=str(args.symbol).upper(),
        input_database=str(args.input_database),
        output_database=str(args.output_database),
        chain_version=str(args.chain_version),
        expected_chain_hash=_sha_text(
            str(args.expected_chain_hash), label="CHAIN_HASH"
        ),
        expected_bronze_records=int(args.expected_bronze_records),
        resume=bool(args.resume),
        start_chain_index=int(args.start_chain_index),
        end_chain_index=int(args.end_chain_index),
        chunk_market_minutes=int(args.chunk_market_minutes),
        warmup_minutes=int(args.warmup_minutes),
        max_rss_mib=int(args.max_rss_mib),
        min_free_disk_gib=float(args.min_free_disk_gib),
        min_available_memory_mib=int(args.min_available_memory_mib),
        progress_every_chunks=int(args.progress_every_chunks),
        report_path=Path(args.report_path),
        lock_path=Path(args.lock_path),
        enforce_canonical_lock_path=not bool(
            getattr(args, "allow_noncanonical_lock_path", False)
        ),
    )


def execute(
    args: argparse.Namespace,
    *,
    client: Any | None = None,
    bronze_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not (
        args.check_only
        or args.run
        or args.verify_only
        or args.init_schema
        or args.epoch_plan_only
    ):
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_EXPLICIT_MODE_REQUIRED")
    if args.resume and not args.run:
        raise SilverBuildError("STOP_SILVER_RESUME_REQUIRES_RUN")
    if (args.check_only or args.verify_only or args.epoch_plan_only) and args.init_schema:
        raise SilverBuildError("STOP_SILVER_RUNNER_SAFETY_READ_ONLY_MODE_WITH_DDL")
    config = config_from_args(args)
    own_clients = False
    if client is None:
        if args.run:
            clients = open_silver_session_clients()
        else:
            # Read-only / schema modes still use a dedicated session, but only one.
            single = get_clickhouse_client(role="admin")
            clients = _as_session_clients(single)
        own_clients = True
    else:
        clients = _as_session_clients(client)
    admin = clients.write
    try:
        if args.check_only:
            report = preflight(admin, config, bronze_probe=bronze_probe)
            print(json.dumps(report, indent=2, sort_keys=True), flush=True)
            return report
        if args.epoch_plan_only:
            preflight_report = preflight(admin, config, bronze_probe=bronze_probe)
            metadata = load_segment_metadata(clients.read, config)
            profile = profile_epoch_plan(clients.read, config, metadata)
            payload = {"preflight": preflight_report, "epoch_plan": profile}
            _write_report(config.report_path, payload)
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
            return payload
        if args.verify_only:
            preflight_report = preflight(
                admin, config, require_output_schema=True, bronze_probe=bronze_probe
            )
            result = verify_build(clients.verify, config)
            payload = {"preflight": preflight_report, "verify": result}
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
            return payload

        stop = StopState()
        previous = _install_signal_handlers(stop)
        try:
            with BuildLock(config.lock_path, _lock_metadata(config)):
                try:
                    preflight_report = preflight(
                        admin,
                        config,
                        require_output_schema=not args.init_schema,
                        check_lock=False,
                        bronze_probe=bronze_probe,
                        require_bronze_ready=bool(args.run or args.verify_only),
                        enforce_bucket_repair_gate=bool(args.run),
                    )
                    if args.init_schema:
                        init_schema(clients.write, config.output_database)
                    if not args.run:
                        payload = {
                            "status": "SCHEMA_INITIALIZED",
                            "preflight": preflight_report,
                        }
                        _write_report(config.report_path, payload)
                        print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
                        return payload
                    if not target_schema_exists(clients.verify, config.output_database):
                        raise SilverBuildError("STOP_SILVER_VERIFY_SCHEMA_MISSING")
                    metadata = load_segment_metadata(clients.read, config)
                    plan = build_epoch_plan(clients.read, config, metadata)
                    _write_report(
                        config.report_path,
                        {
                            "status": "STARTING",
                            "preflight": preflight_report,
                            "epoch_plan_hash": plan.epoch_plan_hash,
                            "epochs": len(plan.epochs),
                            "gaps": len(plan.gaps),
                            "session_ids": clients.session_ids(),
                            "started_at": _now_iso(),
                        },
                    )
                    result = run_build(clients, config, plan, stop=stop)
                    payload = {
                        "verdict": "BTC_SILVER_V1_3_FULL_BUILD_COMPLETE",
                        "preflight": preflight_report,
                        "result": result,
                    }
                    _write_report(config.report_path, payload)
                    return payload
                except BaseException as exc:
                    interrupted = isinstance(exc, ControlledInterrupt)
                    memory_limited = (
                        _is_clickhouse_memory_error(exc)
                        or (
                            isinstance(exc, SilverBuildError)
                            and "STOP_SILVER_CH_MEMORY_LIMIT" in str(exc)
                        )
                        or _is_python_memory_limit(exc)
                    )
                    session_locked = _is_clickhouse_session_locked(exc)
                    status = "INTERRUPTED" if interrupted else "FAILED"
                    if (memory_limited or session_locked) and not interrupted:
                        status = "INTERRUPTED"
                    _write_report(
                        config.report_path,
                        {
                            "status": status,
                            "error": str(exc),
                            "signal": stop.signal_number,
                            "failed_at": _now_iso(),
                            "memory_limited": memory_limited,
                            "session_locked": session_locked,
                            "session_ids": clients.session_ids(),
                        },
                    )
                    if isinstance(exc, SilverBuildError):
                        raise
                    raise _map_clickhouse_operational_error(exc) from exc
        finally:
            _restore_signal_handlers(previous)
    finally:
        if own_clients:
            clients.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        if args.run:
            print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        return 0
    except SilverBuildError as exc:
        print(
            json.dumps(
                {"verdict": str(exc).split(":", 1)[0], "error": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "verdict": "STOP_SILVER_RUNNER_SAFETY_UNEXPECTED",
                    "error": str(exc),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
