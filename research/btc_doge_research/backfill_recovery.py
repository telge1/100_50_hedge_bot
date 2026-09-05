"""Rebuild run state and progress from canonical ClickHouse batch records."""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_json import atomic_write_json, read_json_lenient
from .backfill_plan import build_backfill_plan
from .clickhouse import connect, rows
from .contracts import TARGET_DATABASE, sanitize_json, stable_hash
from .full_history_contracts import (
    IMPORTABLE_MODALITIES,
    RESULT_ROOT_SOURCE_RECOVERY,
    RUN_STATE_DIR,
    segment_batch_id,
    segment_build_id,
)
from .contracts import parse_utc
from .run_state import (
    heartbeat_path,
    progress_path,
    watermarks_path,
)

RECOVERY_ROOT = Path(__file__).resolve().parents[2] / "results" / "btc_doge_research_db_backfill_recovery_v1"


def secure_corrupted_progress() -> dict[str, Any]:
    RECOVERY_ROOT.mkdir(parents=True, exist_ok=True)
    src = progress_path()
    if not src.is_file():
        return {"secured": False, "reason": "MISSING"}
    raw = src.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    dst = RECOVERY_ROOT / "progress.json.corrupted"
    shutil.copy2(src, dst)
    parsed, corrupted = read_json_lenient(src)
    meta = {
        "secured": True,
        "source": str(src),
        "copy": str(dst),
        "sha256": digest,
        "bytes": len(raw),
        "corrupted": corrupted,
        "parsed_completed": parsed.get("completed") if isinstance(parsed, dict) else None,
    }
    (RECOVERY_ROOT / "progress_corruption.json").write_text(
        json.dumps(sanitize_json(meta), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if corrupted:
        trailer = raw.split(b"\n}\n", 1)
        if len(trailer) > 1:
            (RECOVERY_ROOT / "progress.json.trailing_fragment.txt").write_bytes(trailer[1])
    return meta


def _plan_row_keys(plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in plan:
        if row["eligibility"] not in ("ELIGIBLE", "COVERAGE_ONLY"):
            continue
        start = parse_utc(row["segment_start"])
        end = parse_utc(row["segment_end"])
        producer = row.get("producer_id") or "CLICKHOUSE_CANONICAL"
        fingerprint = str(row.get("source_fingerprint") or "")
        if row["modality"] in ("TPO_PROFILE", "VOLUME_PROFILE"):
            build_id = stable_hash(
                {
                    "contract": row["contract_version"],
                    "symbol": row["symbol"],
                    "modality": "PROFILES",
                    "day": start.strftime("%Y-%m-%d"),
                    "build": fingerprint,
                }
            )
        else:
            build_id = segment_build_id(
                row["symbol"],
                row["modality"],
                start,
                end,
                producer,
                fingerprint,
                source_path=str(row.get("source_path") or ""),
            )
        batch_id = segment_batch_id(row["symbol"], row["modality"], start, end, producer)
        out[batch_id] = {**row, "batch_id": batch_id, "build_id": build_id}
    return out


def query_clickhouse_progress(client: Any) -> dict[str, Any]:
    fh_rows = rows(
        client,
        f"""SELECT batch_id, build_id, status, phase, rows_written, output_fingerprint,
                   pilot_start, pilot_end, started_at, completed_at, error
            FROM {TARGET_DATABASE}.research_batch_runs
            WHERE batch_id LIKE 'fh:%'
            ORDER BY started_at, batch_id, status""",
    )
    by_batch: dict[str, list[dict[str, Any]]] = {}
    for (
        batch_id,
        build_id_raw,
        status,
        phase,
        rows_written,
        output_fp,
        seg_start,
        seg_end,
        started_at,
        completed_at,
        error,
    ) in fh_rows:
        build_id = bytes(build_id_raw).decode().rstrip("\x00") if isinstance(build_id_raw, (bytes, bytearray)) else str(build_id_raw)
        by_batch.setdefault(str(batch_id), []).append(
            sanitize_json(
                {
                    "batch_id": str(batch_id),
                    "build_id": build_id,
                    "status": str(status),
                    "phase": str(phase),
                    "rows_written": int(rows_written),
                    "output_fingerprint": str(output_fp),
                    "segment_start": seg_start,
                    "segment_end": seg_end,
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "error": str(error),
                }
            )
        )
    ready: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    running: list[dict[str, Any]] = []
    for batch_id, entries in by_batch.items():
        statuses = {e["status"] for e in entries}
        latest = entries[-1]
        if "READY" in statuses:
            ready.append(next(e for e in entries if e["status"] == "READY"))
        elif "PARTIAL" in statuses:
            partial.append(next(e for e in entries if e["status"] == "PARTIAL"))
        elif "FAILED" in statuses:
            failed.append(next(e for e in entries if e["status"] == "FAILED"))
        elif "RUNNING" in statuses:
            running.append(next(e for e in entries if e["status"] == "RUNNING"))
        else:
            running.append(latest)
    return {
        "ready": ready,
        "partial": partial,
        "failed": failed,
        "running": running,
        "ready_count": len(ready),
        "partial_count": len(partial),
        "failed_count": len(failed),
        "running_count": len(running),
    }


def rebuild_run_state_from_clickhouse(*, write_files: bool = True) -> dict[str, Any]:
    RECOVERY_ROOT.mkdir(parents=True, exist_ok=True)
    plan = build_backfill_plan()
    plan_keys = _plan_row_keys(plan)
    importable = [r for r in plan if r["eligibility"] == "ELIGIBLE" and r["modality"] in IMPORTABLE_MODALITIES]
    coverage_only = [r for r in plan if r["eligibility"] == "COVERAGE_ONLY"]
    client = connect()
    try:
        ch = query_clickhouse_progress(client)
    finally:
        client.close()
    ready_ids = {r["batch_id"] for r in ch["ready"]}
    partial_ids = {r["batch_id"] for r in ch["partial"]}
    failed_ids = {r["batch_id"] for r in ch["failed"]}
    running_ids = {r["batch_id"] for r in ch["running"]}
    open_rows = []
    for row in importable:
        start = parse_utc(row["segment_start"])
        end = parse_utc(row["segment_end"])
        producer = row.get("producer_id") or "CLICKHOUSE_CANONICAL"
        batch_id = segment_batch_id(row["symbol"], row["modality"], start, end, producer)
        if batch_id in ready_ids:
            continue
        if batch_id in partial_ids or batch_id in failed_ids or batch_id in running_ids:
            open_rows.append({**row, "batch_id": batch_id, "recovery_status": "ORPHANED_INCOMPLETE"})
        else:
            open_rows.append({**row, "batch_id": batch_id, "recovery_status": "OPEN"})
    watermarks = {
        entry["batch_id"]: {
            "symbol": plan_keys.get(entry["batch_id"], {}).get("symbol", ""),
            "modality": plan_keys.get(entry["batch_id"], {}).get("modality", ""),
            "segment_start": str(entry["segment_start"]),
            "status": entry["status"],
            "build_id": entry["build_id"],
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        for entry in ch["ready"]
    }
    progress = sanitize_json(
        {
            "status": "RECOVERED",
            "source": "clickhouse",
            "total_inventory_segments": len(plan),
            "coverage_only_segments": len(coverage_only),
            "importable_segments": len(importable),
            "ready_segments": ch["ready_count"],
            "partial_segments": ch["partial_count"],
            "failed_segments": ch["failed_count"],
            "orphaned_running_segments": ch["running_count"],
            "skipped_segments": ch["ready_count"],
            "remaining_segments": len(open_rows),
            "open_segments": open_rows[:50],
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    heartbeat = sanitize_json(
        {
            "status": "STOPPED",
            "reason": "RECOVERED_FROM_CLICKHOUSE",
            "ready_segments": ch["ready_count"],
            "remaining_segments": len(open_rows),
            "importable_segments": len(importable),
            "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
    )
    report = sanitize_json(
        {
            "progress": progress,
            "clickhouse": ch,
            "importable_segments": len(importable),
            "coverage_only_segments": len(coverage_only),
            "remaining_segments": len(open_rows),
        }
    )
    (RECOVERY_ROOT / "clickhouse_progress.json").write_text(
        json.dumps(sanitize_json(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if write_files:
        atomic_write_json(progress_path(), progress)
        atomic_write_json(heartbeat_path(), heartbeat)
        atomic_write_json(watermarks_path(), watermarks)
    return report
