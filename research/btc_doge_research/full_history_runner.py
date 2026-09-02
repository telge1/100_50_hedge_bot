"""Resumable full-history backfill for BTCUSDT and DOGEUSDT."""

from __future__ import annotations

import json
import resource
import shutil
from datetime import datetime, timedelta, timezone
from time import perf_counter, process_time
from typing import Any

from .clickhouse import connect, insert, rows
from .contracts import TARGET_DATABASE, sanitize_json, stable_hash
from .phase2_contracts import BUILD_ID as PILOT_BUILD_ID
from .full_history_contracts import (
    FULL_HISTORY_BUILD_ID,
    FULL_HISTORY_CONTRACT_VERSION,
    LIVE_PRODUCER_ID,
    MIN_DISK_RESERVE_GIB,
    OB_SEMANTICS,
    PILOT_COMPRESSED_BYTES,
    STORAGE_SAFETY_FACTOR,
    day_batch_id,
    day_build_id,
    full_history_contract,
    pilot_batch_id,
)
from .full_history_inventory import build_inventory
from .phase2_day_loader import (
    DayContext,
    day_counts,
    day_output_fingerprint,
    insert_liquidations,
    insert_ob,
    insert_oi,
    insert_profiles,
    insert_trade_buckets,
)
from .phase2_ddl import statements as phase2_ddl
from .clickhouse import validate_write_sql


def _disk_free_gib() -> float:
    usage = shutil.disk_usage("/")
    return usage.free / (1024 ** 3)


def _ready_exists(client: Any, batch_id: str, build_id: str) -> bool:
    found = rows(
        client,
        f"""SELECT count() FROM {TARGET_DATABASE}.research_batch_runs
        WHERE batch_id=%(batch)s AND build_id=%(build)s AND status='READY'""",
        {"batch": batch_id, "build": build_id},
    )
    return bool(found and int(found[0][0]))


def _register_batch(
    client: Any,
    *,
    batch_id: str,
    build_id: str,
    day_start: datetime,
    day_end: datetime,
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
            batch_id, build_id, FULL_HISTORY_CONTRACT_VERSION, day_start, day_end,
            status, phase, stable_hash(full_history_contract()), output_fingerprint,
            rows_written, started, completed, error,
        )],
        (
            "batch_id","build_id","contract_version","pilot_start","pilot_end",
            "status","phase","input_fingerprint","output_fingerprint",
            "rows_written","started_at","completed_at","error",
        ),
    )


def _eligible_symbol_days(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for row in inventory["inventory_rows"]:
        if not row["eligible"]:
            continue
        if row["utc_day"] == "2026-08-26":
            continue
        out.append(row)
    return out


def _storage_gate(eligible_count: int) -> dict[str, Any]:
    expected = int(PILOT_COMPRESSED_BYTES * eligible_count * STORAGE_SAFETY_FACTOR)
    free_gib = _disk_free_gib()
    reserve = MIN_DISK_RESERVE_GIB
    return {
        "eligible_symbol_days": eligible_count,
        "expected_compressed_bytes": expected,
        "free_gib": free_gib,
        "min_reserve_gib": reserve,
        "pass": free_gib >= reserve and free_gib * (1024 ** 3) >= expected,
    }


def _load_symbol_day(client: Any, row: dict[str, Any], started: datetime) -> dict[str, Any]:
    day = datetime.fromisoformat(row["utc_day"]).replace(tzinfo=timezone.utc)
    ctx = DayContext(
        symbol=row["symbol"],
        day_start=day,
        day_end=day + timedelta(days=1),
        batch_id=day_batch_id(row["symbol"], day),
        build_id=day_build_id(row["symbol"], day),
        contract_version=FULL_HISTORY_CONTRACT_VERSION,
        producer_id=row["producer_id"],
        source_semantics_version=row["source_semantics"],
        source_fingerprint=str(row["source_fingerprint"]),
    )
    if _ready_exists(client, ctx.batch_id, ctx.build_id):
        return {"status": "IDEMPOTENT_SKIP", "symbol": ctx.symbol, "day": row["utc_day"], "counts": day_counts(client, ctx)}
    _register_batch(
        client,
        batch_id=ctx.batch_id,
        build_id=ctx.build_id,
        day_start=ctx.day_start,
        day_end=ctx.day_end,
        status="RUNNING",
        phase="START",
        started=started,
    )
    try:
        insert_trade_buckets(client, ctx, started)
        insert_liquidations(client, ctx, started)
        insert_oi(client, ctx, started)
        insert_profiles(client, ctx, started)
        ob_count = insert_ob(client, ctx, started)
        counts = day_counts(client, ctx)
        if counts["research_ob200_snapshots_1s"] != 86400 or ob_count != 86400:
            raise RuntimeError(f"OB count gate failed: {counts}")
        if counts["research_open_interest_observations"] != 17280:
            raise RuntimeError(f"OI count gate failed: {counts}")
        fingerprint = day_output_fingerprint(counts)
        completed = datetime.now(timezone.utc)
        _register_batch(
            client,
            batch_id=ctx.batch_id,
            build_id=ctx.build_id,
            day_start=ctx.day_start,
            day_end=ctx.day_end,
            status="READY",
            phase="COMPLETE",
            started=started,
            completed=completed,
            output_fingerprint=fingerprint,
            rows_written=sum(counts.values()),
        )
        return sanitize_json(
            {
                "status": "READY",
                "symbol": ctx.symbol,
                "day": row["utc_day"],
                "batch_id": ctx.batch_id,
                "build_id": ctx.build_id,
                "counts": counts,
                "output_fingerprint": fingerprint,
                "ob_seconds": ob_count,
            }
        )
    except Exception as exc:
        failed = datetime.now(timezone.utc)
        _register_batch(
            client,
            batch_id=ctx.batch_id,
            build_id=ctx.build_id,
            day_start=ctx.day_start,
            day_end=ctx.day_end,
            status="FAILED",
            phase="ERROR",
            started=started,
            completed=failed,
            error=str(exc)[:1000],
        )
        raise


def run(*, resume_only: bool = False) -> dict[str, Any]:
    wall_start, cpu_start = perf_counter(), process_time()
    inventory = build_inventory()
    eligible = _eligible_symbol_days(inventory)
    gate = _storage_gate(len(eligible))
    if not gate["pass"]:
        return {"status": "BLOCKED", "reason": "STORAGE_GATE", "gate": gate}
    client = connect()
    results: list[dict[str, Any]] = []
    timings: list[dict[str, Any]] = []
    try:
        for sql in phase2_ddl():
            validate_write_sql(sql)
            client.command(sql)
        for row in eligible:
            started = datetime.now(timezone.utc)
            t0 = perf_counter()
            result = _load_symbol_day(client, row, started)
            timings.append(
                {
                    "symbol": row["symbol"],
                    "day": row["utc_day"],
                    "seconds": perf_counter() - t0,
                    "status": result["status"],
                }
            )
            results.append(result)
            if resume_only and result["status"] != "IDEMPOTENT_SKIP":
                break
        pilot_ready = _ready_exists(client, pilot_batch_id(), PILOT_BUILD_ID)
        if not eligible and pilot_ready:
            run_status = "READY"
        elif not eligible:
            run_status = "PARTIAL"
        else:
            run_status = "READY" if all(r["status"] in ("READY", "IDEMPOTENT_SKIP") for r in results) else "PARTIAL"
        return sanitize_json(
            {
                "status": run_status,
                "eligible_symbol_days": len(eligible),
                "loaded": results,
                "timings": timings,
                "storage_gate": gate,
                "pilot_batch_ready": pilot_ready,
                "wall_seconds": perf_counter() - wall_start,
                "cpu_seconds": process_time() - cpu_start,
                "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            }
        )
    finally:
        client.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
