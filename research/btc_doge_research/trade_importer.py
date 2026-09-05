"""Hardened segment import, pilot protocol, and full rematerialization runner."""

from __future__ import annotations

import json
import os
import shutil
import signal
import time
from datetime import datetime, timezone
from typing import Any

from .contracts import TARGET_DATABASE
from .clickhouse import rows
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
from .trade_rematerialization import (
    RESULT_ROOT,
    backup_shifted_state,
    build_plan,
    current_trades_engine,
    ensure_result_root,
    hour_segments,
    quarantine_shifted_table,
    research_command,
    source_stats,
    table_exists,
    write_csv,
    write_json,
    write_rollback_plan,
    write_downstream_lineage_audit,
    _now,
)
from .trade_run_state import (
    RunnerLock,
    status_snapshot,
    update_file_watermark,
    write_heartbeat,
    write_progress,
)


def watermark_row(client: Any, symbol: str, start: datetime) -> dict[str, Any] | None:
    if not table_exists(client, WATERMARK_TABLE):
        return None
    found = rows(
        client,
        f"""
        SELECT
          argMax(status, record_version),
          argMax(source_fingerprint, record_version),
          argMax(rows_written, record_version),
          max(record_version)
        FROM {TARGET_DATABASE}.{WATERMARK_TABLE} FINAL
        WHERE build_id=%(b)s AND symbol=%(s)s AND segment_start=toDateTime64(%(a)s,3,'UTC')
        """,
        {"b": BUILD_ID, "s": symbol, "a": literal_utc(start)},
    )
    if not found or found[0][0] is None:
        return None
    fp = found[0][1]
    if isinstance(fp, (bytes, bytearray)):
        fp = bytes(fp).decode("utf-8", errors="replace").rstrip("\x00")
    return {
        "status": str(found[0][0]),
        "source_fingerprint": str(fp),
        "rows_written": int(found[0][2] or 0),
        "record_version": int(found[0][3] or 0),
    }


def watermark_status(client: Any, symbol: str, start: datetime) -> str | None:
    row = watermark_row(client, symbol, start)
    return None if row is None else row["status"]


def next_record_version(client: Any, symbol: str, start: datetime) -> int:
    row = watermark_row(client, symbol, start)
    return 1 if row is None else int(row["record_version"]) + 1


def write_watermark(
    client: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    status: str,
    source_row_count: int,
    source_unique: int,
    rows_written: int,
    source_fingerprint: str,
    record_version: int,
    started_at: datetime,
    completed_at: datetime | None,
    error: str = "",
) -> None:
    research_command(client, DDL_WATERMARK)
    completed_sql = (
        "NULL"
        if completed_at is None
        else f"toDateTime64('{literal_utc(completed_at)}', 6, 'UTC')"
    )
    err = error.replace("\\", "\\\\").replace("'", "\\'")
    research_command(
        client,
        f"""
        INSERT INTO {TARGET_DATABASE}.{WATERMARK_TABLE}
        (build_id, symbol, segment_start, segment_end, status, source_row_count,
         source_unique_trade_ids, rows_written, source_fingerprint, record_version,
         started_at, completed_at, error)
        SELECT
          '{BUILD_ID}', '{symbol}',
          toDateTime64('{literal_utc(start)}', 3, 'UTC'),
          toDateTime64('{literal_utc(end)}', 3, 'UTC'),
          '{status}', {int(source_row_count)}, {int(source_unique)}, {int(rows_written)},
          '{source_fingerprint}', {int(record_version)},
          toDateTime64('{literal_utc(started_at)}', 6, 'UTC'),
          {completed_sql}, '{err}'
        """,
    )


def import_segment(
    client: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    record_version: int | None = None,
    resume: bool = True,
) -> dict[str, Any]:
    if symbol not in ("BTCUSDT", "DOGEUSDT"):
        raise PermissionError(f"symbol not allowed: {symbol}")
    research_command(client, DDL_WATERMARK)
    research_command(client, DDL_RESEARCH_PUBLIC_TRADES_V2)

    stats = source_stats(client, symbol, start, end)
    fp = source_segment_fingerprint(
        symbol=symbol,
        segment_start=start,
        segment_end=end,
        source_row_count=stats["source_row_count"],
        source_unique_trade_ids=stats["source_unique_trade_ids"],
        min_trade_id=stats["min_trade_id"],
        max_trade_id=stats["max_trade_id"],
    )
    existing = watermark_row(client, symbol, start)
    version = record_version if record_version is not None else next_record_version(client, symbol, start)

    if resume and existing and existing["status"] in {"COMPLETE", "COMPLETE_EMPTY", "PARTIAL"}:
        if existing["source_fingerprint"] == fp:
            parity = segment_parity(client, symbol=symbol, start=start, end=end, use_final=True)
            return {
                "symbol": symbol,
                "segment_start": iso_z(start),
                "segment_end": iso_z(end),
                "status": "IDEMPOTENT_SKIP",
                "rows_written": existing["rows_written"],
                "source_fingerprint": fp,
                "prior_status": existing["status"],
                "parity": parity,
            }
        write_watermark(
            client,
            symbol=symbol,
            start=start,
            end=end,
            status="FAILED",
            source_row_count=stats["source_row_count"],
            source_unique=stats["source_unique_trade_ids"],
            rows_written=0,
            source_fingerprint=fp,
            record_version=version,
            started_at=_now(),
            completed_at=_now(),
            error="SOURCE_FINGERPRINT_CONFLICT",
        )
        raise RuntimeError(
            f"SOURCE_FINGERPRINT_CONFLICT {symbol} {iso_z(start)}: "
            f"stored={existing['source_fingerprint']} current={fp}"
        )

    started = _now()
    batch_id = segment_batch_id(symbol, start)
    a, b = literal_utc(start), literal_utc(end)
    imported_at = literal_utc(started)

    write_watermark(
        client,
        symbol=symbol,
        start=start,
        end=end,
        status="CLAIMED",
        source_row_count=stats["source_row_count"],
        source_unique=stats["source_unique_trade_ids"],
        rows_written=0,
        source_fingerprint=fp,
        record_version=version,
        started_at=started,
        completed_at=None,
    )
    write_watermark(
        client,
        symbol=symbol,
        start=start,
        end=end,
        status="IN_PROGRESS",
        source_row_count=stats["source_row_count"],
        source_unique=stats["source_unique_trade_ids"],
        rows_written=0,
        source_fingerprint=fp,
        record_version=version + 1,
        started_at=started,
        completed_at=None,
    )

    if stats["source_unique_trade_ids"] == 0:
        write_watermark(
            client,
            symbol=symbol,
            start=start,
            end=end,
            status="COMPLETE_EMPTY",
            source_row_count=0,
            source_unique=0,
            rows_written=0,
            source_fingerprint=fp,
            record_version=version + 2,
            started_at=started,
            completed_at=_now(),
        )
        return {
            "symbol": symbol,
            "segment_start": iso_z(start),
            "segment_end": iso_z(end),
            "status": "COMPLETE_EMPTY",
            "rows_written": 0,
            "source_fingerprint": fp,
            "parity": {
                "status": "PASS",
                "source_unique_trade_ids": 0,
                "research_unique_trade_ids": 0,
                "exact_timestamp_matches": 0,
                "shifted_matches": 0,
                "field_mismatches": 0,
                "symbol": symbol,
                "segment_start": iso_z(start),
                "segment_end": iso_z(end),
            },
        }

    sql = f"""
    INSERT INTO {TARGET_DATABASE}.research_public_trades
    (
      symbol, event_time, receive_time, trade_id, price, base_size, quote_notional,
      taker_side, source, source_id, source_fingerprint, source_segment_start,
      source_segment_end, source_contract_version, processor_version, ingestion_batch_id,
      build_id, contract_version, record_version, ingested_at, imported_at,
      quality_flags, coverage_status, finalization_status, event_key
    )
    SELECT
      '{symbol}' AS symbol,
      trade_ts AS event_time,
      ingest_ts AS receive_time,
      trade_id,
      price,
      size AS base_size,
      notional AS quote_notional,
      side AS taker_side,
      source AS source,
      concat('CH_PUBLIC_TRADES_CANONICAL:', source) AS source_id,
      '{fp}' AS source_fingerprint,
      toDateTime64('{a}', 3, 'UTC') AS source_segment_start,
      toDateTime64('{b}', 3, 'UTC') AS source_segment_end,
      'public_trade_taker_aggressor_v1' AS source_contract_version,
      '{TRADE_REMATERIALIZATION_PROCESSOR}' AS processor_version,
      '{batch_id}' AS ingestion_batch_id,
      '{BUILD_ID}' AS build_id,
      '{TRADE_REMATERIALIZATION_CONTRACT_VERSION}' AS contract_version,
      {version + 1} AS record_version,
      toDateTime64('{imported_at}', 6, 'UTC') AS ingested_at,
      toDateTime64('{imported_at}', 6, 'UTC') AS imported_at,
      cast([], 'Array(LowCardinality(String))') AS quality_flags,
      'COMPLETE' AS coverage_status,
      'FINALIZED' AS finalization_status,
      concat('{symbol}', '|', trade_id) AS event_key
    FROM
    (
      SELECT
        trade_id,
        argMax(src.trade_ts, src.ingest_timestamp) AS trade_ts,
        max(src.ingest_timestamp) AS ingest_ts,
        argMax(src.price, src.ingest_timestamp) AS price,
        argMax(src.size, src.ingest_timestamp) AS size,
        argMax(src.notional, src.ingest_timestamp) AS notional,
        argMax(src.side, src.ingest_timestamp) AS side,
        argMax(src.source, src.ingest_timestamp) AS source
      FROM orderbook_analysis.public_trades_canonical AS src
      WHERE src.symbol = '{symbol}'
        AND src.trade_ts >= toDateTime64('{a}', 3, 'UTC')
        AND src.trade_ts < toDateTime64('{b}', 3, 'UTC')
      GROUP BY trade_id
    )
    ORDER BY trade_ts, trade_id
    """
    try:
        research_command(client, sql)
    except Exception as exc:  # noqa: BLE001
        write_watermark(
            client,
            symbol=symbol,
            start=start,
            end=end,
            status="FAILED",
            source_row_count=stats["source_row_count"],
            source_unique=stats["source_unique_trade_ids"],
            rows_written=0,
            source_fingerprint=fp,
            record_version=version + 2,
            started_at=started,
            completed_at=_now(),
            error=str(exc)[:500],
        )
        raise

    parity = segment_parity(client, symbol=symbol, start=start, end=end, use_final=True)
    written = int(parity["research_unique_trade_ids"])
    if parity["status"] != "PASS":
        write_watermark(
            client,
            symbol=symbol,
            start=start,
            end=end,
            status="FAILED",
            source_row_count=stats["source_row_count"],
            source_unique=stats["source_unique_trade_ids"],
            rows_written=written,
            source_fingerprint=fp,
            record_version=version + 2,
            started_at=started,
            completed_at=_now(),
            error=(
                f"parity_fail exact={parity['exact_timestamp_matches']} "
                f"shifted={parity['shifted_matches']} field={parity['field_mismatches']}"
            ),
        )
        raise RuntimeError(f"parity FAILED {symbol} {iso_z(start)}: {parity}")

    write_watermark(
        client,
        symbol=symbol,
        start=start,
        end=end,
        status="COMPLETE",
        source_row_count=stats["source_row_count"],
        source_unique=stats["source_unique_trade_ids"],
        rows_written=written,
        source_fingerprint=fp,
        record_version=version + 2,
        started_at=started,
        completed_at=_now(),
    )
    update_file_watermark(
        f"{symbol}|{iso_z(start)}",
        {
            "status": "COMPLETE",
            "rows_written": written,
            "source_fingerprint": fp,
            "source_min_ts": parity.get("source_min_ts"),
            "source_max_ts": parity.get("source_max_ts"),
            "research_min_ts": parity.get("research_min_ts"),
            "research_max_ts": parity.get("research_max_ts"),
        },
    )
    return {
        "symbol": symbol,
        "segment_start": iso_z(start),
        "segment_end": iso_z(end),
        "status": "COMPLETE",
        "rows_written": written,
        "source_fingerprint": fp,
        "batch_id": batch_id,
        "parity": parity,
        "source_min_ts": parity.get("source_min_ts"),
        "source_max_ts": parity.get("source_max_ts"),
    }


PILOT_SEGMENTS: tuple[tuple[str, datetime, datetime, str], ...] = (
    (
        "BTCUSDT",
        datetime(2026, 8, 31, 18, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        "A_BTC_GOLDEN_18",
    ),
    (
        "BTCUSDT",
        datetime(2026, 8, 31, 19, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 20, 0, tzinfo=timezone.utc),
        "A_BTC_GOLDEN_19",
    ),
    (
        "DOGEUSDT",
        datetime(2026, 8, 31, 13, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 31, 14, 0, tzinfo=timezone.utc),
        "B_DOGE_COMPLETE_13",
    ),
    (
        "DOGEUSDT",
        datetime(2026, 8, 29, 11, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc),
        "C_DOGE_SHIFTED_INVENTORY",
    ),
)


def run_pilot(client: Any, *, pass_label: str = "pass1") -> dict[str, Any]:
    ensure_result_root()
    if not current_trades_engine(client).get("is_v2"):
        backup_shifted_state(client)
        quarantine_shifted_table(client)
    elif not table_exists(client, INVALID_SHIFTED_TABLE):
        raise RuntimeError("invalid shifted quarantine missing")

    results = []
    all_pass = True
    for symbol, start, end, label in PILOT_SEGMENTS:
        out = import_segment(client, symbol, start, end, resume=True)
        parity = out.get("parity") or segment_parity(
            client, symbol=symbol, start=start, end=end, use_final=True
        )
        ok = parity["status"] == "PASS" and out["status"] in {
            "COMPLETE",
            "IDEMPOTENT_SKIP",
            "COMPLETE_EMPTY",
        }
        if not ok:
            all_pass = False
        results.append({"label": label, "import": out, "parity": parity, "ok": ok})

    payload = {
        "pass": pass_label,
        "verdict": "PILOT_PASS" if all_pass else "PILOT_FAIL",
        "segments": results,
        "contract_version": TRADE_REMATERIALIZATION_CONTRACT_VERSION,
        "build_id": BUILD_ID,
        "companion_fallback": False,
    }
    path = RESULT_ROOT / "pilot_results.json"
    prior: dict[str, Any] = {}
    if path.is_file():
        try:
            prior = json.loads(path.read_text())
        except json.JSONDecodeError:
            prior = {}
    passes = [p for p in prior.get("passes", []) if p.get("pass") != pass_label]
    passes.append(payload)
    write_json(
        path,
        {
            "passes": passes,
            "overall_verdict": (
                "PILOT_PASS"
                if all(p.get("verdict") == "PILOT_PASS" for p in passes)
                else "PILOT_FAIL"
            ),
        },
    )
    all_parity = []
    for p in passes:
        for seg in p["segments"]:
            pr = seg["parity"]
            all_parity.append(
                {
                    "pass": p["pass"],
                    "label": seg["label"],
                    "symbol": pr["symbol"],
                    "segment_start": pr["segment_start"],
                    "segment_end": pr["segment_end"],
                    "import_status": seg["import"]["status"],
                    "source_unique": pr["source_unique_trade_ids"],
                    "research_unique": pr["research_unique_trade_ids"],
                    "exact_timestamp_matches": pr["exact_timestamp_matches"],
                    "shifted_matches": pr["shifted_matches"],
                    "field_mismatches": pr["field_mismatches"],
                    "source_min_ts": pr.get("source_min_ts"),
                    "source_max_ts": pr.get("source_max_ts"),
                    "research_min_ts": pr.get("research_min_ts"),
                    "research_max_ts": pr.get("research_max_ts"),
                    "ok": seg["ok"],
                }
            )
    write_csv(RESULT_ROOT / "pilot_parity.csv", list(all_parity[0].keys()), all_parity)
    return payload


def run_pilot_twice(client: Any) -> dict[str, Any]:
    pass1 = run_pilot(client, pass_label="pass1")
    pass2 = run_pilot(client, pass_label="pass2")
    idem = {
        "pass1_statuses": [s["import"]["status"] for s in pass1["segments"]],
        "pass2_statuses": [s["import"]["status"] for s in pass2["segments"]],
        "pass2_all_idempotent_skip": all(
            s["import"]["status"] == "IDEMPOTENT_SKIP" for s in pass2["segments"]
        ),
        "pass1_verdict": pass1["verdict"],
        "pass2_verdict": pass2["verdict"],
        "verdict": (
            "IDEMPOTENCY_PASS"
            if pass1["verdict"] == "PILOT_PASS"
            and pass2["verdict"] == "PILOT_PASS"
            and all(s["import"]["status"] == "IDEMPOTENT_SKIP" for s in pass2["segments"])
            else "IDEMPOTENCY_FAIL"
        ),
    }
    write_json(RESULT_ROOT / "pilot_idempotency.json", idem)
    return {
        "pass1": pass1,
        "pass2": pass2,
        "idempotency": idem,
        "verdict": (
            "PILOT_PASS"
            if pass1["verdict"] == "PILOT_PASS"
            and pass2["verdict"] == "PILOT_PASS"
            and idem["verdict"] == "IDEMPOTENCY_PASS"
            else "PILOT_FAIL"
        ),
    }


def build_full_backfill_plan(client: Any) -> dict[str, Any]:
    ensure_result_root()
    plan = build_plan(client)
    disk = shutil.disk_usage("/")
    expected = int(plan["approx_unique_trades"])
    done = 0
    if table_exists(client, WATERMARK_TABLE):
        done = int(
            rows(
                client,
                f"""
                SELECT sum(rows_written) FROM {TARGET_DATABASE}.{WATERMARK_TABLE} FINAL
                WHERE build_id=%(b)s AND status IN ('COMPLETE','COMPLETE_EMPTY')
                """,
                {"b": BUILD_ID},
            )[0][0]
            or 0
        )
    remaining = max(0, expected - done)
    est_seconds = remaining / 2000.0 if remaining else 0
    payload = {
        "segment_count": plan["segment_count"],
        "nonempty_segments": plan["nonempty_segments"],
        "expected_source_unique_trades": expected,
        "already_written_watermark_rows": done,
        "remaining_approx": remaining,
        "estimated_target_bytes_gb": round(expected * 200 / (1024**3), 2),
        "disk_free_gb": round(disk.free / (1024**3), 2),
        "estimated_runtime_seconds": round(est_seconds, 1),
        "estimated_runtime_minutes": round(est_seconds / 60.0, 1),
        "safe_batch": "1 UTC hour / segment",
        "estimated_ram_mb": "<512 (server-side INSERT SELECT)",
        "nohup_required": est_seconds > 600,
        "build_id": BUILD_ID,
        "contract_version": TRADE_REMATERIALIZATION_CONTRACT_VERSION,
        "history_start": iso_z(HISTORY_START),
        "history_end": iso_z(HISTORY_END),
    }
    write_json(RESULT_ROOT / "full_backfill_plan.json", payload)
    return payload


def run_full(
    client: Any,
    *,
    resume: bool = True,
    symbols: tuple[str, ...] = ("BTCUSDT", "DOGEUSDT"),
    launcher_pid: int | None = None,
) -> dict[str, Any]:
    ensure_result_root()
    lock = RunnerLock()
    acquired = lock.acquire(launcher_pid=launcher_pid)
    if not acquired.get("acquired"):
        raise RuntimeError(f"runner lock not acquired: {acquired}")

    stop = {"flag": False}

    def _on_term(signum, frame):  # noqa: ANN001, ARG001
        stop["flag"] = True
        write_heartbeat({"status": "STOPPING", "runner_pid": os.getpid()})

    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    try:
        if not current_trades_engine(client).get("is_v2"):
            backup_shifted_state(client)
            quarantine_shifted_table(client)

        plan = build_full_backfill_plan(client)
        write_heartbeat(
            {
                "status": "RUNNING",
                "runner_pid": os.getpid(),
                "launcher_pid": launcher_pid,
                "build_id": BUILD_ID,
            }
        )
        completed = skipped = failed_n = 0
        rows_written = 0
        t0 = time.time()
        failed: list[dict[str, Any]] = []

        for symbol in symbols:
            for start, end in hour_segments():
                if stop["flag"]:
                    write_heartbeat({"status": "STOPPED", "runner_pid": os.getpid()})
                    summary = {
                        "status": "STOPPED",
                        "completed": completed,
                        "skipped": skipped,
                        "rows_written": rows_written,
                        "failed": failed,
                        "build_id": BUILD_ID,
                    }
                    write_json(RESULT_ROOT / "full_backfill_status.json", summary)
                    write_progress(summary)
                    return summary
                try:
                    out = import_segment(client, symbol, start, end, resume=resume)
                    if out["status"] == "IDEMPOTENT_SKIP":
                        skipped += 1
                    elif out["status"] in {"COMPLETE", "COMPLETE_EMPTY"}:
                        completed += 1
                        rows_written += int(out.get("rows_written") or 0)
                    else:
                        completed += 1
                    elapsed = max(time.time() - t0, 1e-6)
                    rate = rows_written / elapsed
                    rem = max(0, int(plan["remaining_approx"]) - rows_written)
                    eta = rem / rate if rate > 0 else None
                    progress = {
                        "status": "RUNNING",
                        "symbol": symbol,
                        "segment_start": iso_z(start),
                        "completed": completed,
                        "skipped": skipped,
                        "rows_written": rows_written,
                        "rows_per_sec": round(rate, 1),
                        "eta_seconds": None if eta is None else round(eta, 1),
                        "build_id": BUILD_ID,
                    }
                    write_progress(progress)
                    write_heartbeat({**progress, "runner_pid": os.getpid()})
                    write_json(RESULT_ROOT / "full_backfill_status.json", progress)
                except Exception as exc:  # noqa: BLE001
                    failed_n += 1
                    failed.append(
                        {
                            "symbol": symbol,
                            "segment_start": iso_z(start),
                            "error": str(exc)[:500],
                        }
                    )
                    write_heartbeat(
                        {
                            "status": "FAILED",
                            "runner_pid": os.getpid(),
                            "failed_segment": f"{symbol}|{iso_z(start)}",
                            "error": str(exc)[:500],
                        }
                    )
                    summary = {
                        "status": "FAILED",
                        "completed": completed,
                        "skipped": skipped,
                        "rows_written": rows_written,
                        "failed": failed,
                        "build_id": BUILD_ID,
                    }
                    write_json(RESULT_ROOT / "full_backfill_status.json", summary)
                    write_progress(summary)
                    raise

        summary = {
            "status": "COMPLETED",
            "completed": completed,
            "skipped": skipped,
            "rows_written": rows_written,
            "failed": failed,
            "finished_at": iso_z(_now()),
            "build_id": BUILD_ID,
        }
        write_json(RESULT_ROOT / "full_backfill_status.json", summary)
        write_progress(summary)
        write_heartbeat({"status": "COMPLETED", "runner_pid": os.getpid(), **summary})
        return summary
    finally:
        lock.release()


def status_report(client: Any) -> dict[str, Any]:
    ensure_result_root()
    engine = current_trades_engine(client)
    wm = []
    if table_exists(client, WATERMARK_TABLE):
        wm = [
            {
                "symbol": r[0],
                "status": r[1],
                "segments": int(r[2]),
                "rows_written": int(r[3]),
            }
            for r in rows(
                client,
                f"""
                SELECT symbol, status, count(), sum(rows_written)
                FROM {TARGET_DATABASE}.{WATERMARK_TABLE} FINAL
                WHERE build_id=%(b)s
                GROUP BY symbol, status
                ORDER BY symbol, status
                """,
                {"b": BUILD_ID},
            )
        ]
    research_count = 0
    if engine.get("exists"):
        research_count = int(
            rows(
                client,
                f"SELECT count() FROM {TARGET_DATABASE}.research_public_trades FINAL",
            )[0][0]
        )
    payload = {
        "build_id": BUILD_ID,
        "engine": engine,
        "watermarks": wm,
        "research_public_trades_rows_final": research_count,
        "invalid_shifted_present": table_exists(client, INVALID_SHIFTED_TABLE),
        "run_state": status_snapshot(),
    }
    write_json(RESULT_ROOT / "status.json", payload)
    write_json(RESULT_ROOT / "full_backfill_status.json", payload)
    return payload


def audit_report(client: Any) -> dict[str, Any]:
    ensure_result_root()
    samples = []
    for symbol, start, end, _label in PILOT_SEGMENTS:
        samples.append(segment_parity(client, symbol=symbol, start=start, end=end, use_final=True))
    dist = []
    if current_trades_engine(client).get("exists"):
        dist = shift_distribution(
            client,
            research_table=f"{TARGET_DATABASE}.research_public_trades",
            use_final=True,
        )
    payload = {
        "segment_audits": samples,
        "shift_distribution": dist,
        "all_pass": all(s["status"] == "PASS" for s in samples),
    }
    write_json(RESULT_ROOT / "audit_result.json", payload)
    return payload


def compute_canonical_invariants(client: Any) -> dict[str, Any]:
    """Post-repair invariants on FINAL research_public_trades for rematerialized build."""
    ensure_result_root()
    dup = int(
        rows(
            client,
            f"""
            SELECT count() FROM (
              SELECT symbol, trade_id, count() c
              FROM {TARGET_DATABASE}.research_public_trades FINAL
              WHERE build_id=%(b)s
              GROUP BY symbol, trade_id HAVING c > 1
            )
            """,
            {"b": BUILD_ID},
        )[0][0]
    )
    shifted = 0
    # Sample join for shift on rematerialized hours with watermarks COMPLETE
    shifted = int(
        rows(
            client,
            f"""
            SELECT count()
            FROM {TARGET_DATABASE}.research_public_trades AS r FINAL
            INNER JOIN (
              SELECT src.symbol AS symbol, src.trade_id AS trade_id,
                     argMax(src.trade_ts, src.ingest_timestamp) AS trade_ts
              FROM orderbook_analysis.public_trades_canonical AS src
              GROUP BY src.symbol, src.trade_id
            ) AS o ON r.symbol=o.symbol AND r.trade_id=o.trade_id
            WHERE r.build_id=%(b)s
              AND dateDiff('second', o.trade_ts, r.event_time) != 0
            """,
            {"b": BUILD_ID},
        )[0][0]
    )
    stale_active = int(
        rows(
            client,
            f"""
            SELECT count() FROM {TARGET_DATABASE}.{WATERMARK_TABLE} FINAL
            WHERE build_id=%(b)s AND status IN ('CLAIMED','IN_PROGRESS')
            """,
            {"b": BUILD_ID},
        )[0][0]
    )
    payload = {
        "duplicate_canonical_trade_keys": dup,
        "shifted_canonical_timestamps": shifted,
        "stale_ACTIVE_batch": stale_active,
        "build_id": BUILD_ID,
        "pass": dup == 0 and shifted == 0 and stale_active == 0,
    }
    write_json(RESULT_ROOT / "canonical_invariants.json", payload)
    return payload


# Re-export helpers expected by CLI
__all__ = [
    "PILOT_SEGMENTS",
    "audit_report",
    "build_full_backfill_plan",
    "compute_canonical_invariants",
    "import_segment",
    "run_full",
    "run_pilot",
    "run_pilot_twice",
    "status_report",
    "watermark_status",
    "write_downstream_lineage_audit",
    "write_rollback_plan",
    "build_plan",
]
