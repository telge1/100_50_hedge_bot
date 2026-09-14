"""Append-only repair for missing terminal 100ms Silver states (v1.3).

Default mode is read-only. Productive DML requires explicit ``--run``.
Never rewrites Level-Changes. Never deletes existing states. Never runs while
the full Silver builder holds ``build.lock``.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .epoch_aware_silver_v1_3 import (
    EpochDefinition,
    EpochSilverError,
    INSERT_BATCH_MAX_BYTES,
    INSERT_BATCH_SIZE,
    analysis_bucket_count,
    epoch_apply_bounds,
    evaluation_end_ns,
    iter_analysis_bucket_starts,
    replay_epoch_window,
    split_resume_and_replay_stream,
    state_row_id,
    _hash,
    _text,
)
from .helpers import get_clickhouse_client
from .silver_full_build_v1_3 import (
    BRONZE_PAYLOAD_FULL,
    BuildConfig,
    BuildLock,
    CHUNKS_TABLE,
    ControlledInterrupt,
    DEFAULT_INPUT_DATABASE,
    DEFAULT_LOCK_PATH,
    DEFAULT_OUTPUT_DATABASE,
    EXPECTED_BRANCH,
    LEVEL_CHANGES_TABLE,
    METRICS_TABLE,
    SilverBuildError,
    SilverSessionClients,
    StopState,
    _as_session_clients,
    _check_resources,
    _close_streaming_iterator,
    _flush_insert_rows,
    _install_signal_handlers,
    _now_iso,
    _ns_iso,
    _restore_signal_handlers,
    bronze_hard_preflight,
    build_epoch_plan,
    code_identity,
    iter_bronze_records,
    load_segment_metadata,
    open_silver_session_clients,
    validate_input_database,
)

RUNNER_VERSION = "btc_silver_bucket_boundary_repair_v1_3"
REPAIR_SCHEMA_VERSION = "silver_bucket_boundary_repair_v1_3"
DEFAULT_REPAIR_LOCK_PATH = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/repair.lock"
)
DEFAULT_REPAIR_REPORT = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/silver_bucket_boundary_repair_v1_3/report.json"
)
REPAIR_RUNS_TABLE = "silver_bucket_repair_runs_v1_3"
PILOT_OUTPUT_PREFIX = "research_full_ob_silver_pilot_"


class BucketRepairError(RuntimeError):
    """Fail-closed repair error with a stable STOP_* / status verdict."""


@dataclass
class AffectedChunk:
    chunk_key: str
    build_id: str
    run_id: str
    epoch_id: str
    epoch_hash: str
    epoch_plan_hash: str
    chain_version: str
    canonical_chain_hash: str
    symbol: str
    chunk_start_ns: int
    chunk_end_ns: int
    warmup_ns: int
    level_change_count: int
    state_count: int
    source_record_count: int
    output_hash: str
    version_ms: int
    expected_state_count: int
    missing_bucket_starts: list[int]


def _validate_output_database(database: str, *, allow_pilot: bool) -> None:
    if not database or not database.replace("_", "a").isalnum():
        raise BucketRepairError("STOP_REPAIR_OUTPUT_DATABASE_IDENTIFIER")
    if database == DEFAULT_OUTPUT_DATABASE:
        return
    if allow_pilot and database.startswith(PILOT_OUTPUT_PREFIX):
        return
    raise BucketRepairError(
        "STOP_REPAIR_OUTPUT_DATABASE_NOT_ALLOWED: "
        "only production Silver v1.3 or research_full_ob_silver_pilot_* "
        f"(got {database})"
    )


def _assert_builder_idle() -> None:
    if DEFAULT_LOCK_PATH.exists():
        raise BucketRepairError(
            "STOP_REPAIR_FULL_BUILDER_ACTIVE: productive build.lock is held"
        )
    import subprocess

    try:
        out = subprocess.check_output(
            ["pgrep", "-af", "silver_full_build_v1_3"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        return
    live = [
        line
        for line in out.splitlines()
        if "silver_full_build_v1_3" in line and "--run" in line
    ]
    if live:
        raise BucketRepairError(
            "STOP_REPAIR_FULL_BUILDER_ACTIVE: silver_full_build_v1_3 --run process found"
        )


def ensure_repair_schema(client: Any, database: str) -> None:
    client.command(f"CREATE DATABASE IF NOT EXISTS {database}")
    client.command(
        f"""
        CREATE TABLE IF NOT EXISTS {database}.{REPAIR_RUNS_TABLE}
        (
            repair_run_id FixedString(64),
            runner_version String,
            repair_schema_version String,
            chain_version String,
            canonical_chain_hash FixedString(64),
            symbol LowCardinality(String),
            output_database String,
            status LowCardinality(String),
            chunks_total UInt64,
            chunks_repaired UInt64,
            chunks_skipped UInt64,
            host String,
            pid UInt64,
            report_json String,
            version_ms UInt64
        )
        ENGINE = ReplacingMergeTree(version_ms)
        ORDER BY repair_run_id
        SETTINGS index_granularity = 8192
        """.strip()
    )


def load_complete_chunk_rows(
    client: Any,
    *,
    database: str,
    chain_version: str,
) -> list[dict[str, Any]]:
    rows = client.query(
        f"""
        SELECT
            chunk_key, build_id, run_id, epoch_id, epoch_hash, epoch_plan_hash,
            chain_version, canonical_chain_hash, symbol,
            chunk_start_ns, chunk_end_ns, warmup_ns,
            level_change_count, state_count, source_record_count,
            output_hash, version_ms
        FROM {database}.{CHUNKS_TABLE} FINAL
        WHERE status = 'COMPLETE'
          AND chain_version = {{cv:String}}
        ORDER BY chunk_start_ns, chunk_key
        """,
        parameters={"cv": chain_version},
    ).result_rows
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "chunk_key": _text(row[0]),
                "build_id": _text(row[1]),
                "run_id": _text(row[2]),
                "epoch_id": _text(row[3]),
                "epoch_hash": _text(row[4]),
                "epoch_plan_hash": _text(row[5]),
                "chain_version": _text(row[6]),
                "canonical_chain_hash": _text(row[7]),
                "symbol": _text(row[8]),
                "chunk_start_ns": int(row[9]),
                "chunk_end_ns": int(row[10]),
                "warmup_ns": int(row[11]),
                "level_change_count": int(row[12]),
                "state_count": int(row[13]),
                "source_record_count": int(row[14]),
                "output_hash": _text(row[15]),
                "version_ms": int(row[16]),
            }
        )
    return out


def load_epoch_map(
    client: Any,
    config: BuildConfig,
) -> dict[str, EpochDefinition]:
    """Rebuild canonical epochs with runtime apply_end bounds (not persisted)."""
    metadata = load_segment_metadata(client, config)
    plan = build_epoch_plan(client, config, metadata)
    return {epoch.epoch_id: epoch for epoch in plan.epochs}


def existing_state_index(
    client: Any,
    *,
    database: str,
    chunk_key: str,
) -> dict[int, tuple[str, str]]:
    """Map bucket_start_ns -> (row_id, book_hash from payload)."""
    rows = client.query(
        f"""
        SELECT row_id, bucket_start_ns, payload
        FROM {database}.{METRICS_TABLE} FINAL
        WHERE chunk_key = {{chunk_key:String}}
        """,
        parameters={"chunk_key": chunk_key},
    ).result_rows
    index: dict[int, tuple[str, str]] = {}
    for row_id, bucket_ns, payload in rows:
        book_hash = ""
        try:
            parsed = json.loads(payload)
            book_hash = str(parsed.get("book_hash") or "")
        except (TypeError, json.JSONDecodeError):
            book_hash = ""
        key = int(bucket_ns)
        rid = _text(row_id)
        if key in index and index[key][0] != rid:
            raise BucketRepairError(
                f"STOP_REPAIR_DUPLICATE_BUCKET_CONFLICT: chunk={chunk_key} bucket={key}"
            )
        index[key] = (rid, book_hash)
    return index


def discover_affected_chunks(
    client: Any,
    *,
    database: str,
    chain_version: str,
) -> list[AffectedChunk]:
    affected: list[AffectedChunk] = []
    for row in load_complete_chunk_rows(
        client, database=database, chain_version=chain_version
    ):
        expected = analysis_bucket_count(row["chunk_start_ns"], row["chunk_end_ns"])
        planned = list(
            iter_analysis_bucket_starts(row["chunk_start_ns"], row["chunk_end_ns"])
        )
        existing = existing_state_index(
            client, database=database, chunk_key=row["chunk_key"]
        )
        missing = [ns for ns in planned if ns not in existing]
        if not missing and row["state_count"] == expected and len(existing) == expected:
            continue
        if missing or row["state_count"] != expected or len(existing) != expected:
            affected.append(
                AffectedChunk(
                    expected_state_count=expected,
                    missing_bucket_starts=missing,
                    **row,
                )
            )
    return affected


def count_unrepaired_production_holes(client: Any, *, chain_version: str) -> int:
    """Fast production gate used by the full builder resume path."""
    return int(
        client.query(
            f"""
            SELECT count()
            FROM {DEFAULT_OUTPUT_DATABASE}.{CHUNKS_TABLE} FINAL
            WHERE status = 'COMPLETE'
              AND chain_version = {{cv:String}}
              AND state_count < toUInt64(
                    greatest(
                        intDiv(
                            toInt64(chunk_end_ns) - toInt64(
                                intDiv(chunk_start_ns + 99999999, 100000000) * 100000000
                            ),
                            100000000
                        ),
                        0
                    )
              )
            """,
            parameters={"cv": chain_version},
        ).result_rows[0][0]
    )


def assert_production_bucket_repair_verified(
    client: Any,
    *,
    chain_version: str,
    output_database: str,
) -> None:
    if output_database != DEFAULT_OUTPUT_DATABASE:
        return
    # Exact detection via planner semantics for COMPLETE rows.
    holes = 0
    for row in load_complete_chunk_rows(
        client, database=output_database, chain_version=chain_version
    ):
        expected = analysis_bucket_count(row["chunk_start_ns"], row["chunk_end_ns"])
        if int(row["state_count"]) != expected:
            holes += 1
    if holes:
        raise SilverBuildError(
            "STOP_SILVER_FULL_RESUME_UNTIL_REPAIR_VERIFIED: "
            f"unrepaired_complete_chunks={holes}"
        )


def _replay_chunk_for_repair(
    clients: SilverSessionClients,
    config: BuildConfig,
    *,
    epoch: EpochDefinition,
    chunk_start_ns: int,
    chunk_end_ns: int,
    build_id: str,
    stop: StopState,
) -> Any:
    start_apply, end_apply = epoch_apply_bounds(epoch)
    bronze_stream = None
    try:
        bronze_stream = iter_bronze_records(
            clients.read,
            config,
            start_apply_key=start_apply,
            end_apply_key=end_apply,
            payload_mode=BRONZE_PAYLOAD_FULL,
        )
        resume_record, replay_stream = split_resume_and_replay_stream(
            bronze_stream,
            epoch=epoch,
            analysis_start_ns=chunk_start_ns,
        )
        stop.check()
        return replay_epoch_window(
            replay_stream,
            epoch=epoch,
            resume_record=resume_record,
            analysis_start_ns=chunk_start_ns,
            analysis_end_ns=chunk_end_ns,
            build_id=build_id,
            retain_level_changes=False,
            retain_states=True,
        )
    except EpochSilverError as exc:
        raise BucketRepairError(str(exc)) from exc
    finally:
        if bronze_stream is not None:
            _close_streaming_iterator(bronze_stream)


def repair_one_chunk(
    clients: SilverSessionClients,
    config: BuildConfig,
    *,
    affected: AffectedChunk,
    epoch: EpochDefinition,
    stop: StopState,
    dry_run: bool,
) -> dict[str, Any]:
    expected = affected.expected_state_count
    existing = existing_state_index(
        clients.verify,
        database=config.output_database,
        chunk_key=affected.chunk_key,
    )
    if (
        not affected.missing_bucket_starts
        and len(existing) == expected
        and affected.state_count == expected
    ):
        return {
            "status": "SKIPPED_ALREADY_REPAIRED",
            "chunk_key": affected.chunk_key,
            "inserted_states": 0,
            "state_count": expected,
            "level_change_count": affected.level_change_count,
        }

    replay = _replay_chunk_for_repair(
        clients,
        config,
        epoch=epoch,
        chunk_start_ns=affected.chunk_start_ns,
        chunk_end_ns=affected.chunk_end_ns,
        build_id=affected.build_id,
        stop=stop,
    )
    replay_by_ns = {
        int(row["bucket_start_ms"]) * 1_000_000: row for row in replay.states
    }
    if len(replay_by_ns) != expected:
        raise BucketRepairError(
            "STOP_REPAIR_REPLAY_COUNT_MISMATCH: "
            f"{len(replay_by_ns)}!={expected} chunk={affected.chunk_key}"
        )

    # Verify LC count unchanged in store.
    lc_store = int(
        clients.verify.query(
            f"""
            SELECT count()
            FROM {config.output_database}.{LEVEL_CHANGES_TABLE} FINAL
            WHERE chunk_key = {{chunk_key:String}}
            """,
            parameters={"chunk_key": affected.chunk_key},
        ).result_rows[0][0]
    )
    if lc_store != affected.level_change_count:
        raise BucketRepairError(
            "STOP_REPAIR_LC_COUNT_DRIFT: "
            f"store={lc_store} ledger={affected.level_change_count}"
        )
    if int(replay.level_change_count or 0) != affected.level_change_count:
        raise BucketRepairError(
            "STOP_REPAIR_LC_REPLAY_MISMATCH: "
            f"replay={replay.level_change_count} ledger={affected.level_change_count}"
        )

    to_insert: list[dict[str, Any]] = []
    for bucket_ns, row in sorted(replay_by_ns.items()):
        row_id = state_row_id(chunk_key=affected.chunk_key, bucket_start_ns=bucket_ns)
        book_hash = str(row.get("book_hash") or "")
        if bucket_ns in existing:
            existing_id, existing_hash = existing[bucket_ns]
            if existing_id != row_id:
                raise BucketRepairError(
                    "STOP_REPAIR_ROW_ID_CONFLICT: "
                    f"chunk={affected.chunk_key} bucket={bucket_ns}"
                )
            if existing_hash and book_hash and existing_hash != book_hash:
                raise BucketRepairError(
                    "STOP_REPAIR_EXISTING_STATE_CONFLICT: "
                    f"chunk={affected.chunk_key} bucket={_ns_iso(bucket_ns)}"
                )
            continue
        to_insert.append(row)

    if dry_run:
        return {
            "status": "WOULD_REPAIR",
            "chunk_key": affected.chunk_key,
            "missing_buckets": [_ns_iso(ns) for ns in affected.missing_bucket_starts],
            "would_insert": len(to_insert),
            "expected_state_count": expected,
            "evaluation_end_ns": replay.evaluation_end_ns,
        }

    version_ms = max(int(time.time() * 1000), affected.version_ms + 1)
    if to_insert:
        columns = [
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
        buffers: list[list[Any]] = [[] for _ in columns]
        nbytes = 0
        insert_calls = 0

        def flush() -> None:
            nonlocal nbytes, insert_calls
            if not buffers[0]:
                return
            _flush_insert_rows(
                clients.write,
                table=f"{config.output_database}.{METRICS_TABLE}",
                column_names=columns,
                columns=buffers,
            )
            insert_calls += 1
            for buf in buffers:
                buf.clear()
            nbytes = 0

        for row in to_insert:
            bucket_ns = int(row["bucket_start_ms"]) * 1_000_000
            payload = json.dumps(row, separators=(",", ":"))
            if buffers[0] and (
                len(buffers[0]) >= INSERT_BATCH_SIZE
                or nbytes + len(payload) > INSERT_BATCH_MAX_BYTES
            ):
                flush()
            values = [
                state_row_id(chunk_key=affected.chunk_key, bucket_start_ns=bucket_ns),
                affected.build_id,
                affected.chunk_key,
                affected.epoch_id,
                affected.epoch_hash,
                affected.chain_version,
                affected.canonical_chain_hash,
                affected.symbol,
                bucket_ns,
                payload,
                version_ms,
            ]
            for buf, value in zip(buffers, values):
                buf.append(value)
            nbytes += len(payload)
        flush()
    else:
        insert_calls = 0

    # Verify FINAL counts.
    st_row = clients.verify.query(
        f"""
        SELECT count(), uniqExact(row_id), uniqExact(bucket_start_ns)
        FROM {config.output_database}.{METRICS_TABLE} FINAL
        WHERE chunk_key = {{chunk_key:String}}
        """,
        parameters={"chunk_key": affected.chunk_key},
    ).result_rows[0]
    st = (int(st_row[0]), int(st_row[1]), int(st_row[2]))
    if st[0] != expected or st[1] != expected or st[2] != expected:
        raise BucketRepairError(
            "STOP_REPAIR_VERIFY_STATE_MISMATCH: "
            f"count={st[0]} uniq_row={st[1]} uniq_bucket={st[2]} expected={expected}"
        )
    lc_after = int(
        clients.verify.query(
            f"""
            SELECT count()
            FROM {config.output_database}.{LEVEL_CHANGES_TABLE} FINAL
            WHERE chunk_key = {{chunk_key:String}}
            """,
            parameters={"chunk_key": affected.chunk_key},
        ).result_rows[0][0]
    )
    if lc_after != affected.level_change_count:
        raise BucketRepairError(
            f"STOP_REPAIR_LC_MUTATED: before={affected.level_change_count} after={lc_after}"
        )

    output_hash = _hash(
        {
            "level_change_apply_hash": replay.level_change_hash_apply_order,
            "level_change_count": affected.level_change_count,
            "state_count": expected,
            "last_apply_key": replay.end_apply_key,
        }
    )
    clients.write.insert(
        f"{config.output_database}.{CHUNKS_TABLE}",
        [[
            affected.chunk_key,
            affected.build_id,
            affected.run_id,
            affected.epoch_id,
            affected.epoch_hash,
            affected.epoch_plan_hash,
            affected.chain_version,
            affected.canonical_chain_hash,
            affected.symbol,
            affected.chunk_start_ns,
            affected.chunk_end_ns,
            affected.warmup_ns,
            "COMPLETE",
            f"REPAIRED_BUCKET_BOUNDARY:{REPAIR_SCHEMA_VERSION}",
            affected.level_change_count,
            expected,
            affected.source_record_count,
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
    return {
        "status": "REPAIRED",
        "chunk_key": affected.chunk_key,
        "inserted_states": len(to_insert),
        "insert_calls_states": insert_calls,
        "state_count": expected,
        "level_change_count": affected.level_change_count,
        "output_hash": output_hash,
        "missing_buckets": [_ns_iso(ns) for ns in affected.missing_bucket_starts],
        "evaluation_end_ns": replay.evaluation_end_ns,
        "eval_end_needed": evaluation_end_ns(
            affected.chunk_end_ns, epoch.safe_end_ns
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Append-only Silver v1.3 terminal-bucket repair runner"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--pilot", action="store_true")
    mode.add_argument("--run", action="store_true")
    mode.add_argument("--verify-only", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--input-database", default=DEFAULT_INPUT_DATABASE)
    parser.add_argument("--output-database", default=DEFAULT_OUTPUT_DATABASE)
    parser.add_argument("--chain-version", required=True)
    parser.add_argument("--expected-chain-hash", required=True)
    parser.add_argument("--expected-bronze-records", type=int, default=2_638_997)
    parser.add_argument("--max-rss-mib", type=int, default=1536)
    parser.add_argument("--min-free-disk-gib", type=float, default=200.0)
    parser.add_argument("--min-available-memory-mib", type=int, default=4096)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPAIR_REPORT)
    parser.add_argument("--lock-path", type=Path, default=DEFAULT_REPAIR_LOCK_PATH)
    parser.add_argument("--limit-chunks", type=int, default=0)
    return parser


def config_from_args(args: argparse.Namespace) -> BuildConfig:
    return BuildConfig(
        symbol=str(args.symbol).upper(),
        input_database=str(args.input_database),
        output_database=str(args.output_database),
        chain_version=str(args.chain_version),
        expected_chain_hash=str(args.expected_chain_hash).lower(),
        expected_bronze_records=int(args.expected_bronze_records),
        resume=bool(args.resume),
        start_chain_index=0,
        end_chain_index=163,
        chunk_market_minutes=15,
        warmup_minutes=0,
        max_rss_mib=int(args.max_rss_mib),
        min_free_disk_gib=float(args.min_free_disk_gib),
        min_available_memory_mib=int(args.min_available_memory_mib),
        progress_every_chunks=1,
        report_path=Path(args.report_path),
        lock_path=Path(args.lock_path),
        enforce_canonical_lock_path=False,
    )


def execute(
    args: argparse.Namespace,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    allow_pilot = bool(args.pilot) or str(args.output_database).startswith(
        PILOT_OUTPUT_PREFIX
    )
    _validate_output_database(args.output_database, allow_pilot=allow_pilot)
    validate_input_database(args.input_database)
    identity = code_identity()
    if identity["branch"] != EXPECTED_BRANCH:
        raise BucketRepairError("STOP_REPAIR_WRONG_BRANCH")
    if args.run and args.output_database == DEFAULT_OUTPUT_DATABASE:
        _assert_builder_idle()
    if args.resume and not (args.run or args.pilot):
        raise BucketRepairError("STOP_REPAIR_RESUME_REQUIRES_RUN_OR_PILOT")

    config = config_from_args(args)
    own = False
    if client is None:
        if args.run or args.pilot:
            clients = open_silver_session_clients()
        else:
            clients = _as_session_clients(get_clickhouse_client(role="repair-ro"))
        own = True
    else:
        clients = _as_session_clients(client)
    try:
        clients.write.command("SET max_threads = 1")
        _check_resources(clients.verify, config)
        bronze = bronze_hard_preflight(clients.verify, config)
        affected = discover_affected_chunks(
            clients.verify,
            database=config.output_database,
            chain_version=config.chain_version,
        )
        if args.limit_chunks and args.limit_chunks > 0:
            affected = affected[: int(args.limit_chunks)]
        inventory = {
            "affected_chunks": len(affected),
            "missing_states_total": sum(len(a.missing_bucket_starts) for a in affected),
            "sample": [
                {
                    "chunk_key": a.chunk_key,
                    "window": f"{_ns_iso(a.chunk_start_ns)}..{_ns_iso(a.chunk_end_ns)}",
                    "ledger_state_count": a.state_count,
                    "expected_state_count": a.expected_state_count,
                    "missing": [_ns_iso(ns) for ns in a.missing_bucket_starts[:5]],
                    "missing_count": len(a.missing_bucket_starts),
                }
                for a in affected[:10]
            ],
        }
        if args.check_only:
            payload = {
                "verdict": "REPAIR_CHECK_ONLY",
                "mode": "READ_ONLY",
                "code": identity,
                "bronze": bronze,
                "inventory": inventory,
            }
            args.report_path.parent.mkdir(parents=True, exist_ok=True)
            args.report_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
            return payload

        if args.verify_only:
            still = discover_affected_chunks(
                clients.verify,
                database=config.output_database,
                chain_version=config.chain_version,
            )
            if still:
                raise BucketRepairError(
                    "STOP_REPAIR_VERIFY_FAILED: "
                    f"remaining_affected_chunks={len(still)}"
                )
            payload = {
                "verdict": "REPAIR_VERIFIED",
                "affected_remaining": 0,
                "code": identity,
            }
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)
            return payload

        # --pilot or --run
        stop = StopState()
        previous = _install_signal_handlers(stop)
        repaired = 0
        skipped = 0
        results: list[dict[str, Any]] = []
        try:
            with BuildLock(
                config.lock_path,
                {
                    "pid": os.getpid(),
                    "started_at": _now_iso(),
                    "host": socket.gethostname(),
                    "mode": "pilot" if args.pilot else "run",
                    "output_database": config.output_database,
                    "branch": identity["branch"],
                    "head": identity["head"],
                },
            ):
                if args.run and config.output_database == DEFAULT_OUTPUT_DATABASE:
                    _assert_builder_idle()
                ensure_repair_schema(clients.write, config.output_database)
                epoch_map = load_epoch_map(clients.read, config)
                for item in affected:
                    stop.check()
                    epoch = epoch_map.get(item.epoch_id)
                    if epoch is None:
                        raise BucketRepairError(
                            f"STOP_REPAIR_EPOCH_MISSING: {item.epoch_id}"
                        )
                    if epoch.epoch_hash != item.epoch_hash:
                        raise BucketRepairError(
                            "STOP_REPAIR_EPOCH_HASH_DRIFT: "
                            f"{item.epoch_id}"
                        )
                    result = repair_one_chunk(
                        clients,
                        config,
                        affected=item,
                        epoch=epoch,
                        stop=stop,
                        dry_run=False,
                    )
                    results.append(result)
                    print(json.dumps(result, sort_keys=True), flush=True)
                    if result["status"] == "SKIPPED_ALREADY_REPAIRED":
                        skipped += 1
                    else:
                        repaired += 1
                remaining = discover_affected_chunks(
                    clients.verify,
                    database=config.output_database,
                    chain_version=config.chain_version,
                )
                if remaining:
                    raise BucketRepairError(
                        "STOP_REPAIR_INCOMPLETE: "
                        f"remaining_affected_chunks={len(remaining)}"
                    )
                payload = {
                    "verdict": "BUCKET_BOUNDARY_REPAIR_COMPLETE",
                    "mode": "PILOT" if args.pilot else "RUN",
                    "chunks_total": len(affected),
                    "chunks_repaired": repaired,
                    "chunks_skipped": skipped,
                    "results": results,
                    "code": identity,
                    "finished_at": _now_iso(),
                }
                args.report_path.parent.mkdir(parents=True, exist_ok=True)
                args.report_path.write_text(
                    json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                return payload
        except ControlledInterrupt as exc:
            raise BucketRepairError(f"INTERRUPTED: {exc}") from exc
        finally:
            _restore_signal_handlers(previous)
    finally:
        if own:
            clients.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = execute(args)
        if args.run or args.pilot:
            print(json.dumps(result, indent=2, sort_keys=True), flush=True)
        return 0
    except (BucketRepairError, SilverBuildError, ControlledInterrupt) as exc:
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
                {"verdict": "STOP_REPAIR_UNEXPECTED", "error": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
