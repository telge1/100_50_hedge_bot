"""Resumable modality-scoped full-history backfill for BTCUSDT and DOGEUSDT."""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import sys
import uuid
from datetime import datetime, timedelta, timezone
from time import perf_counter, process_time
from typing import Any

from .backfill_plan import build_backfill_plan, run as generate_plan
from .backfill_recovery import rebuild_run_state_from_clickhouse, secure_corrupted_progress
from .clickhouse import connect, insert, rows, validate_write_sql
from .contracts import TARGET_DATABASE, parse_utc, sanitize_json, stable_hash
from .full_history_contracts import (
    FULL_HISTORY_CONTRACT_VERSION,
    IMPORTABLE_MODALITIES,
    MIN_DISK_RESERVE_GIB,
    ModalityContractError,
    PILOT_DAY_STR,
    STORAGE_SAFETY_FACTOR,
    full_history_contract,
    segment_batch_id,
    segment_build_id,
)
from .phase2_ddl import statements as phase2_ddl
from .run_state import (
    acquire_runner_lock,
    mark_failed,
    read_progress,
    release_runner_lock,
    status_snapshot,
    update_watermark,
    write_heartbeat,
    write_progress,
)
from .ob200_segments import SourceVanishedError, validate_source_file_at_batch
from .segment_loader import (
    SegmentContext,
    load_segment,
    segment_counts,
    segment_output_fingerprint,
)
from .source_discovery import run as discover_sources

_STOP_REQUESTED = False
TERMINAL_BATCH_STATUSES = frozenset({"READY", "PARTIAL"})
CLAIM_STALE_AFTER = timedelta(minutes=30)


def _disk_free_gib() -> float:
    return shutil.disk_usage("/").free / (1024 ** 3)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return parse_utc(value.replace(" ", "T") if "T" not in value else value)


def _handle_sigterm(_signum: int, _frame: Any) -> None:
    global _STOP_REQUESTED
    _STOP_REQUESTED = True
    write_heartbeat({"status": "STOPPING", "runner_pid": os.getpid()})


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8").rstrip("\x00")
    return str(value).rstrip("\x00")


def _terminal_exists(client: Any, batch_id: str, build_id: str) -> bool:
    """True when a READY or PARTIAL terminal row exists for this batch/build."""
    found = rows(
        client,
        f"""SELECT count() FROM {TARGET_DATABASE}.research_batch_runs
        WHERE batch_id=%(batch)s AND build_id=%(build)s
          AND status IN ('READY', 'PARTIAL')""",
        {"batch": batch_id, "build": build_id},
    )
    return bool(found and int(found[0][0]))


def _ready_exists(client: Any, batch_id: str, build_id: str) -> bool:
    """Backward-compatible alias; terminal READY/PARTIAL both skip reimport."""
    return _terminal_exists(client, batch_id, build_id)


def _foreign_build_conflict(client: Any, batch_id: str, build_id: str) -> str | None:
    """Return conflicting build_id if the same batch already terminalized under another build."""
    found = rows(
        client,
        f"""SELECT build_id FROM {TARGET_DATABASE}.research_batch_runs
        WHERE batch_id=%(batch)s AND status IN ('READY', 'PARTIAL')
        GROUP BY build_id""",
        {"batch": batch_id},
    )
    for row in found:
        other = _text(row[0])
        if other != build_id:
            return other
    return None


def _existing_build_rows(client: Any, ctx: SegmentContext) -> int:
    return int(sum(segment_counts(client, ctx).values()))


def _claim_token(error: str) -> str | None:
    if error.startswith("claim:"):
        return error[6:]
    return None


def decide_claim_winner(
    claims: list[tuple[datetime, str]],
    *,
    now: datetime,
    stale_after: timedelta = CLAIM_STALE_AFTER,
) -> str | None:
    """Pick the single active writer claim token.

    Among non-stale claims, the earliest (started_at, token) wins. If all claims
    are stale, the newest claim wins so crashed writers can be resumed.
    """
    if not claims:
        return None
    fresh = [(ts, tok) for ts, tok in claims if now - ts <= stale_after]
    pool = fresh if fresh else claims
    return sorted(pool, key=lambda item: (item[0], item[1]))[0][1]


def _register_running_claim(
    client: Any,
    *,
    ctx: SegmentContext,
    started: datetime,
    claim_token: str,
) -> None:
    _register_batch(
        client,
        batch_id=ctx.batch_id,
        build_id=ctx.build_id,
        segment_start=ctx.segment_start,
        segment_end=ctx.segment_end,
        status="RUNNING",
        phase=ctx.modality,
        started=started,
        error=f"claim:{claim_token}",
    )


def _list_running_claims(
    client: Any, batch_id: str, build_id: str
) -> list[tuple[datetime, str]]:
    found = rows(
        client,
        f"""SELECT started_at, error FROM {TARGET_DATABASE}.research_batch_runs
        WHERE batch_id=%(batch)s AND build_id=%(build)s AND status='RUNNING'""",
        {"batch": batch_id, "build": build_id},
    )
    out: list[tuple[datetime, str]] = []
    for started_at, error in found:
        token = _claim_token(_text(error))
        if not token:
            continue
        ts = started_at
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append((ts, token))
    return out


def _coverage_status_from_existing(client: Any, ctx: SegmentContext) -> str:
    if ctx.modality != "OB200":
        return "COMPLETE"
    found = rows(
        client,
        f"""SELECT any(coverage_status) FROM {TARGET_DATABASE}.research_ob200_snapshots_1s
        WHERE build_id=%(build)s
          AND snapshot_ts >= %(start)s AND snapshot_ts < %(end)s""",
        {
            "build": ctx.build_id,
            "start": ctx.segment_start,
            "end": ctx.segment_end,
        },
    )
    if found and found[0][0]:
        return str(found[0][0])
    return "PARTIAL" if ctx.expected_rows else "COMPLETE"


def _finalize_terminal(
    client: Any,
    *,
    ctx: SegmentContext,
    row: dict[str, Any],
    started: datetime,
    result: dict[str, Any],
    batch_status: str,
) -> dict[str, Any]:
    fingerprint = segment_output_fingerprint(client, ctx)
    completed = datetime.now(timezone.utc)
    _register_batch(
        client,
        batch_id=ctx.batch_id,
        build_id=ctx.build_id,
        segment_start=ctx.segment_start,
        segment_end=ctx.segment_end,
        status=batch_status,
        phase="COMPLETE",
        started=started,
        completed=completed,
        output_fingerprint=fingerprint,
        rows_written=int(result.get("rows", 0)),
    )
    update_watermark(
        ctx.batch_id,
        {
            "symbol": ctx.symbol,
            "modality": ctx.modality,
            "segment_start": row["segment_start"],
            "status": batch_status,
            "build_id": ctx.build_id,
        },
    )
    return sanitize_json(
        {
            "status": batch_status,
            "symbol": ctx.symbol,
            "modality": ctx.modality,
            "segment_start": row["segment_start"],
            "batch_id": ctx.batch_id,
            "build_id": ctx.build_id,
            "result": result,
            "output_fingerprint": fingerprint,
        }
    )


def _batch_status_for_result(ctx: SegmentContext, result: dict[str, Any]) -> str:
    if ctx.modality in ("OPEN_INTEREST", "OB200") and result.get("coverage_status") == "PARTIAL":
        return "PARTIAL"
    return "READY"


def _assert_importable_modality(modality: str) -> None:
    if modality not in IMPORTABLE_MODALITIES:
        raise ModalityContractError(
            f"modality {modality} is not importable (COVERAGE_ONLY or excluded)"
        )


def _register_batch(
    client: Any,
    *,
    batch_id: str,
    build_id: str,
    segment_start: datetime,
    segment_end: datetime,
    status: str,
    phase: str,
    started: datetime,
    completed: datetime | None = None,
    output_fingerprint: str = "0" * 64,
    rows_written: int = 0,
    error: str = "",
) -> None:
    insert(
        client,
        "research_batch_runs",
        [(
            batch_id, build_id, FULL_HISTORY_CONTRACT_VERSION, segment_start, segment_end,
            status, phase, stable_hash(full_history_contract()), output_fingerprint,
            rows_written, started, completed, error,
        )],
        (
            "batch_id", "build_id", "contract_version", "pilot_start", "pilot_end",
            "status", "phase", "input_fingerprint", "output_fingerprint",
            "rows_written", "started_at", "completed_at", "error",
        ),
    )


def _plan_row_to_context(row: dict[str, Any]) -> SegmentContext:
    _assert_importable_modality(row["modality"])
    start = parse_utc(row["segment_start"])
    end = parse_utc(row["segment_end"])
    producer = row.get("producer_id") or "CLICKHOUSE_CANONICAL"
    semantics = row.get("source_semantics", "public_trade_taker_aggressor_v1")
    if row["modality"] == "OB200":
        semantics = "raw_ob200_event_time_eos_v1"
    fingerprint = str(row.get("source_fingerprint") or "")
    source_path = str(row.get("source_path") or "")
    build_id = segment_build_id(
        row["symbol"], row["modality"], start, end, producer, fingerprint,
        source_path=source_path if row["modality"] == "OB200" else "",
    )
    if row["modality"] in ("TPO_PROFILE", "VOLUME_PROFILE"):
        day = start.strftime("%Y-%m-%d")
        build_id = stable_hash(
            {
                "contract": FULL_HISTORY_CONTRACT_VERSION,
                "symbol": row["symbol"],
                "modality": "PROFILES",
                "day": day,
                "build": fingerprint,
            }
        )
    batch_id = segment_batch_id(row["symbol"], row["modality"], start, end, producer)
    return SegmentContext(
        symbol=row["symbol"],
        modality=row["modality"],
        segment_start=start,
        segment_end=end,
        batch_id=batch_id,
        build_id=build_id,
        contract_version=FULL_HISTORY_CONTRACT_VERSION,
        producer_id=producer,
        source_semantics_version=semantics,
        source_fingerprint=fingerprint,
        source_path=source_path,
        expected_rows=int(row.get("expected_rows") or 0),
        boundary_auxiliary_path=str(row.get("boundary_auxiliary_path") or ""),
        boundary_auxiliary_fingerprint=str(row.get("boundary_auxiliary_fingerprint") or ""),
    )


def _filter_plan(
    plan: list[dict[str, Any]],
    *,
    symbol: str | None,
    start: datetime | None,
    end: datetime | None,
    pilot_only: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in plan:
        if not row.get("import_eligible", row["eligibility"] == "ELIGIBLE" and row["modality"] in IMPORTABLE_MODALITIES):
            continue
        if row["segment_start"][:10] == PILOT_DAY_STR:
            continue
        if symbol and row["symbol"] != symbol:
            continue
        seg_start = parse_utc(row["segment_start"])
        if start and seg_start < start:
            continue
        if end and seg_start >= end:
            continue
        if pilot_only:
            if row["symbol"] != pilot_only["symbol"]:
                continue
            if row["modality"] != pilot_only["modality"]:
                continue
            if row["segment_start"] != pilot_only["segment_start"]:
                continue
        out.append(row)
    return out


def _storage_gate(plan: list[dict[str, Any]]) -> dict[str, Any]:
    expected = int(sum(r.get("expected_bytes", 0) for r in plan) * STORAGE_SAFETY_FACTOR)
    free_gib = _disk_free_gib()
    return {
        "expected_compressed_bytes": expected,
        "free_gib": free_gib,
        "min_reserve_gib": MIN_DISK_RESERVE_GIB,
        "pass": free_gib >= MIN_DISK_RESERVE_GIB and free_gib * (1024 ** 3) >= expected,
        "segments": len(plan),
    }


def _segment_source_gap_failure(
    client: Any,
    row: dict[str, Any],
    started: datetime,
    exc: Exception,
) -> dict[str, Any]:
    ctx = _plan_row_to_context(row)
    failed = datetime.now(timezone.utc)
    _register_batch(
        client,
        batch_id=ctx.batch_id,
        build_id=ctx.build_id,
        segment_start=ctx.segment_start,
        segment_end=ctx.segment_end,
        status="FAILED",
        phase="SOURCE_GAP",
        started=started,
        completed=failed,
        error=str(exc)[:1000],
    )
    return sanitize_json(
        {
            "status": "FAILED",
            "reason": "SOURCE_GAP",
            "symbol": ctx.symbol,
            "modality": ctx.modality,
            "segment_start": row["segment_start"],
            "batch_id": ctx.batch_id,
            "build_id": ctx.build_id,
            "error": str(exc)[:1000],
        }
    )


def _attempt_load_segment(client: Any, row: dict[str, Any], started: datetime) -> dict[str, Any]:
    if row.get("modality") == "OB200":
        try:
            validate_source_file_at_batch(row)
        except SourceVanishedError:
            raise
        except FileNotFoundError as exc:
            return _segment_source_gap_failure(client, row, started, exc)
    try:
        return _load_segment(client, row, started)
    except SourceVanishedError:
        raise
    except FileNotFoundError as exc:
        return _segment_source_gap_failure(client, row, started, exc)


def _idempotent_skip_payload(ctx: SegmentContext, row: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return sanitize_json(
        {
            "status": "IDEMPOTENT_SKIP",
            "symbol": ctx.symbol,
            "modality": ctx.modality,
            "segment_start": row["segment_start"],
            "batch_id": ctx.batch_id,
            "build_id": ctx.build_id,
            **extra,
        }
    )


def _load_segment(client: Any, row: dict[str, Any], started: datetime) -> dict[str, Any]:
    _assert_importable_modality(row["modality"])
    ctx = _plan_row_to_context(row)

    conflict = _foreign_build_conflict(client, ctx.batch_id, ctx.build_id)
    if conflict:
        raise RuntimeError(
            f"CONFLICT batch_id={ctx.batch_id} build_id={ctx.build_id} "
            f"existing_terminal_build_id={conflict}"
        )

    if _terminal_exists(client, ctx.batch_id, ctx.build_id):
        return _idempotent_skip_payload(ctx, row, reason="TERMINAL_EXISTS")

    existing_rows = _existing_build_rows(client, ctx)
    if existing_rows > 0:
        # Crash after data write / before terminal, or audit reimport attempt.
        coverage = _coverage_status_from_existing(client, ctx)
        recovered = {
            "rows": existing_rows,
            "coverage_status": coverage,
            "status": "IDEMPOTENT_SKIP",
            "recovered_from_existing_rows": True,
        }
        batch_status = _batch_status_for_result(ctx, recovered)
        finalized = _finalize_terminal(
            client,
            ctx=ctx,
            row=row,
            started=started,
            result=recovered,
            batch_status=batch_status,
        )
        finalized["status"] = "IDEMPOTENT_SKIP"
        finalized["reason"] = "EXISTING_ROWS_RECOVERED"
        finalized["terminal_status"] = batch_status
        return sanitize_json(finalized)

    claim_token = uuid.uuid4().hex
    _register_running_claim(client, ctx=ctx, started=started, claim_token=claim_token)
    winner = decide_claim_winner(
        _list_running_claims(client, ctx.batch_id, ctx.build_id),
        now=datetime.now(timezone.utc),
    )
    if winner != claim_token:
        return _idempotent_skip_payload(
            ctx, row, reason="ALREADY_RUNNING", claim_token=claim_token, winner=winner
        )

    # Another writer may have terminalized while we claimed.
    if _terminal_exists(client, ctx.batch_id, ctx.build_id):
        return _idempotent_skip_payload(ctx, row, reason="TERMINAL_RACE")
    existing_rows = _existing_build_rows(client, ctx)
    if existing_rows > 0:
        return _idempotent_skip_payload(
            ctx, row, reason="ROWS_RACE", rows=existing_rows
        )

    try:
        result = load_segment(client, ctx, started)
        if result.get("status") == "IDEMPOTENT_SKIP":
            coverage = result.get("coverage_status") or _coverage_status_from_existing(client, ctx)
            recovered = {**result, "coverage_status": coverage}
            batch_status = _batch_status_for_result(ctx, recovered)
            finalized = _finalize_terminal(
                client,
                ctx=ctx,
                row=row,
                started=started,
                result=recovered,
                batch_status=batch_status,
            )
            finalized["status"] = "IDEMPOTENT_SKIP"
            finalized["reason"] = "LOADER_IDEMPOTENT_SKIP"
            finalized["terminal_status"] = batch_status
            return sanitize_json(finalized)
        batch_status = _batch_status_for_result(ctx, result)
        return _finalize_terminal(
            client,
            ctx=ctx,
            row=row,
            started=started,
            result=result,
            batch_status=batch_status,
        )
    except Exception as exc:
        failed = datetime.now(timezone.utc)
        _register_batch(
            client,
            batch_id=ctx.batch_id,
            build_id=ctx.build_id,
            segment_start=ctx.segment_start,
            segment_end=ctx.segment_end,
            status="FAILED",
            phase="ERROR",
            started=started,
            completed=failed,
            error=str(exc)[:1000],
        )
        raise


def run_backfill(
    *,
    resume: bool = False,
    dry_run: bool = False,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    pilot_row: dict[str, Any] | None = None,
    acquire: bool = True,
    max_segments: int | None = None,
    launcher_pid: int | None = None,
) -> dict[str, Any]:
    global _STOP_REQUESTED
    _STOP_REQUESTED = False
    wall_start, cpu_start = perf_counter(), process_time()
    signal.signal(signal.SIGTERM, _handle_sigterm)
    if acquire:
        lock = acquire_runner_lock(launcher_pid=launcher_pid)
        if not lock.get("acquired"):
            return {"status": "BLOCKED", "reason": "ALREADY_RUNNING", "lock": lock}
    plan = build_backfill_plan()
    work = _filter_plan(plan, symbol=symbol, start=start, end=end, pilot_only=pilot_row)
    if max_segments is not None:
        work = work[:max_segments]
    gate = _storage_gate(work)
    if not gate["pass"]:
        if acquire:
            release_runner_lock()
        return {"status": "BLOCKED", "reason": "STORAGE_GATE", "gate": gate}
    if dry_run:
        if acquire:
            release_runner_lock()
        return {"status": "DRY_RUN", "segments": len(work), "gate": gate, "first": work[:5]}
    client = connect()
    results: list[dict[str, Any]] = []
    processed = 0
    skipped = 0
    failed = 0
    run_status = "COMPLETED"
    last_error: Exception | None = None
    last_row: dict[str, Any] | None = None
    try:
        for sql in phase2_ddl():
            validate_write_sql(sql)
            client.command(sql)
        write_progress(
            {
                "status": "RUNNING",
                "total_inventory_segments": len(plan),
                "importable_segments": len(work),
                "ready_segments": 0,
                "skipped_segments": 0,
                "failed_segments": 0,
                "remaining_segments": len(work),
                "completed": 0,
                "current_segment": None,
            }
        )
        write_heartbeat(
            {
                "status": "RUNNING",
                "runner_pid": os.getpid(),
                "importable_segments": len(work),
                "ready_segments": 0,
                "skipped_segments": 0,
                "failed_segments": 0,
                "remaining_segments": len(work),
            }
        )
        for row in work:
            if _STOP_REQUESTED:
                run_status = "STOPPED"
                break
            last_row = row
            ctx = _plan_row_to_context(row)
            if resume and _terminal_exists(client, ctx.batch_id, ctx.build_id):
                item = {"status": "IDEMPOTENT_SKIP", **row}
                results.append(item)
                processed += 1
                skipped += 1
                continue
            if row["modality"] == "OB200":
                write_heartbeat({"status": "RUNNING", "current_modality": "OB200", "current_segment": row})
            started = datetime.now(timezone.utc)
            t0 = perf_counter()
            result = _attempt_load_segment(client, row, started)
            results.append(result)
            processed += 1
            if result["status"] == "IDEMPOTENT_SKIP":
                skipped += 1
            elif result["status"] == "FAILED":
                failed += 1
                continue
            write_progress(
                sanitize_json(
                    {
                        "status": "RUNNING" if not _STOP_REQUESTED else "STOPPING",
                        "total_inventory_segments": len(plan),
                        "importable_segments": len(work),
                        "ready_segments": processed - skipped,
                        "skipped_segments": skipped,
                        "failed_segments": failed,
                        "remaining_segments": max(len(work) - processed, 0),
                        "completed": processed,
                        "current_segment": row,
                        "last_result": result,
                        "elapsed_seconds": perf_counter() - t0,
                    }
                )
            )
            write_heartbeat(
                {
                    "status": "RUNNING" if not _STOP_REQUESTED else "STOPPING",
                    "runner_pid": os.getpid(),
                    "importable_segments": len(work),
                    "ready_segments": processed - skipped,
                    "skipped_segments": skipped,
                    "failed_segments": failed,
                    "remaining_segments": max(len(work) - processed, 0),
                    "completed": processed,
                }
            )
    except Exception as exc:
        run_status = "FAILED"
        last_error = exc
        failed += 1
        mark_failed(
            error=exc,
            failed_modality=str(last_row.get("modality", "")) if last_row else "",
            failed_segment=str(last_row.get("segment_start", "")) if last_row else "",
            last_safe_watermark=read_progress(),
        )
        write_progress(
            sanitize_json(
                {
                    "status": "FAILED",
                    "failed_segments": failed,
                    "completed": processed,
                    "last_error": str(exc)[:1000],
                    "last_segment": last_row,
                }
            )
        )
        raise
    finally:
        client.close()
        if acquire:
            if run_status == "COMPLETED" and not _STOP_REQUESTED:
                write_heartbeat(
                    {
                        "status": "COMPLETED",
                        "runner_pid": os.getpid(),
                        "importable_segments": len(work),
                        "ready_segments": processed - skipped,
                        "skipped_segments": skipped,
                        "failed_segments": failed,
                        "remaining_segments": max(len(work) - processed, 0),
                        "completed": processed,
                    }
                )
                write_progress(
                    sanitize_json(
                        {
                            "status": "COMPLETED",
                            "ready_segments": processed - skipped,
                            "skipped_segments": skipped,
                            "failed_segments": failed,
                            "remaining_segments": max(len(work) - processed, 0),
                            "completed": processed,
                        }
                    )
                )
            elif run_status == "STOPPED":
                write_heartbeat({"status": "STOPPED", "runner_pid": os.getpid(), "completed": processed})
                write_progress({"status": "STOPPED", "completed": processed})
            release_runner_lock()
    payload = sanitize_json(
        {
            "status": run_status,
            "segments_processed": processed,
            "skipped": skipped,
            "loaded": results[-20:],
            "gate": gate,
            "wall_seconds": perf_counter() - wall_start,
            "cpu_seconds": process_time() - cpu_start,
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    )
    if last_error is not None:
        payload["error"] = str(last_error)
    return payload


def run_pilot() -> dict[str, Any]:
    from .full_history_contracts import SHADOW_ARCHIVE_PRODUCER_ID
    from .phase2_day_loader import ob_source

    hour = parse_utc("2026-08-31T18:00:00Z")
    source = ob_source("BTCUSDT", hour)
    pilot_row = {
        "symbol": "BTCUSDT",
        "modality": "OB200",
        "segment_start": "2026-08-31T18:00:00Z",
        "segment_end": "2026-08-31T19:00:00Z",
        "producer_id": SHADOW_ARCHIVE_PRODUCER_ID,
        "source": "filesystem_ob200_shadow",
        "source_path": source.relative_path,
        "source_fingerprint": source.fingerprint,
        "source_semantics": "raw_ob200_event_time_eos_v1",
        "expected_rows": 3600,
        "import_eligible": True,
        "eligibility": "ELIGIBLE",
    }
    client = connect()
    started = datetime.now(timezone.utc)
    try:
        for sql in phase2_ddl():
            validate_write_sql(sql)
            client.command(sql)
        first = _load_segment(client, pilot_row, started)
        second = _load_segment(client, pilot_row, datetime.now(timezone.utc))
    finally:
        client.close()
    return sanitize_json(
        {
            "first_run": first,
            "second_run": second,
            "pilot_segment": pilot_row,
            "idempotent": second.get("status") == "IDEMPOTENT_SKIP",
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BTC/DOGE research full-history backfill")
    parser.add_argument("--plan", action="store_true", help="Generate discovery, coverage and plan")
    parser.add_argument("--run", action="store_true", help="Execute backfill plan")
    parser.add_argument("--resume", action="store_true", help="Skip READY segments")
    parser.add_argument("--status", action="store_true", help="Show run state")
    parser.add_argument("--pilot", action="store_true", help="Run controlled pilot segment")
    parser.add_argument("--rebuild-state", action="store_true", help="Rebuild progress from ClickHouse")
    parser.add_argument("--secure-corrupted", action="store_true", help="Secure corrupted progress.json copy")
    parser.add_argument("--dry-run", action="store_true", help="Show work without loading")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--from", dest="from_dt", default=None)
    parser.add_argument("--to", dest="to_dt", default=None)
    parser.add_argument("--max-segments", type=int, default=None)
    args = parser.parse_args(argv)
    if args.secure_corrupted:
        print(json.dumps(secure_corrupted_progress(), indent=2, sort_keys=True))
        return 0
    if args.rebuild_state:
        print(json.dumps(rebuild_run_state_from_clickhouse(), indent=2, sort_keys=True))
        return 0
    if args.plan:
        discover_sources()
        payload = generate_plan()
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if args.status:
        print(json.dumps(status_snapshot(), indent=2, sort_keys=True))
        return 0
    if args.pilot:
        print(json.dumps(run_pilot(), indent=2, sort_keys=True))
        return 0
    if args.run or args.dry_run:
        launcher_pid = int(os.environ.get("LAUNCHER_PID", "0")) or None
        payload = run_backfill(
            resume=args.resume,
            dry_run=args.dry_run,
            symbol=args.symbol,
            start=_parse_dt(args.from_dt),
            end=_parse_dt(args.to_dt),
            max_segments=args.max_segments,
            launcher_pid=launcher_pid,
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0 if payload.get("status") not in ("BLOCKED", "FAILED") else 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
