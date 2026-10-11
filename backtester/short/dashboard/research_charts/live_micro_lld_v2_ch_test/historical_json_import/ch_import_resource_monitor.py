"""Disk + ClickHouse resource observation for historical import batches (read-only)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .ch_import_state import read_ch_import_preflight


def _disk_usage_for_path(path: Path) -> dict[str, int]:
    usage = shutil.disk_usage(path)
    return {
        "path": str(path),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
    }


def collect_resource_baseline(
    catalog_root: Path,
    *,
    pipeline_line_id: str,
    scope_snapshot_ids: set[str] | None = None,
    disk_check_path: Path | None = None,
) -> dict[str, Any]:
    """
    Snapshot CH + host disk before/after batches.

    Limit enforcement is **opt-in** via environment variables (see ``check_optional_limits``).
    """
    disk_path = disk_check_path or Path(
        os.environ.get("HISTORICAL_IMPORT_DISK_CHECK_PATH", str(catalog_root))
    )
    baseline: dict[str, Any] = {
        "disk": _disk_usage_for_path(disk_path),
        "clickhouse": None,
        "clickhouse_error": None,
    }
    try:
        baseline["clickhouse"] = read_ch_import_preflight(
            pipeline_line_id=pipeline_line_id,
            scope_snapshot_ids=scope_snapshot_ids,
        )
    except Exception as exc:  # noqa: BLE001 — report without blocking dry-run
        baseline["clickhouse_error"] = str(exc)
    return baseline


def check_optional_limits(baseline: dict[str, Any]) -> list[str]:
    """
    Return human-readable violations when optional env limits are set.

    Env (all optional — unset means no check):
    - ``HISTORICAL_IMPORT_MIN_FREE_BYTES``
    - ``HISTORICAL_IMPORT_MAX_ACTIVE_MERGES``
    - ``HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS``
    """
    errors: list[str] = []
    min_free = os.environ.get("HISTORICAL_IMPORT_MIN_FREE_BYTES")
    if min_free:
        free = int(baseline["disk"]["free_bytes"])
        need = int(min_free)
        if free < need:
            errors.append(f"disk free {free} < HISTORICAL_IMPORT_MIN_FREE_BYTES={need}")
    ch = baseline.get("clickhouse")
    if not ch:
        return errors
    max_merges = os.environ.get("HISTORICAL_IMPORT_MAX_ACTIVE_MERGES")
    if max_merges:
        merging = int(ch["parts_and_merges"]["active_merges_lmv2"])
        cap = int(max_merges)
        if merging > cap:
            errors.append(
                f"active merges {merging} > HISTORICAL_IMPORT_MAX_ACTIVE_MERGES={cap}"
            )
    max_orphans = os.environ.get("HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS")
    if max_orphans:
        orphans = len(ch.get("orphan_member_snapshots") or [])
        cap = int(max_orphans)
        if orphans > cap:
            errors.append(
                f"orphan member snapshots {orphans} > "
                f"HISTORICAL_IMPORT_MAX_ORPHAN_MEMBER_SNAPSHOTS={cap}"
            )
    max_parts = os.environ.get("HISTORICAL_IMPORT_MAX_LMV2_ACTIVE_PARTS")
    if max_parts:
        parts = ch.get("parts_and_merges", {}).get("parts_by_table") or []
        total_parts = sum(int(p.get("part_count", 0)) for p in parts)
        cap = int(max_parts)
        if total_parts > cap:
            errors.append(
                f"lmv2 active parts {total_parts} > "
                f"HISTORICAL_IMPORT_MAX_LMV2_ACTIVE_PARTS={cap} "
                "(signal_generator.research_lmv2_* only)"
            )
    return errors


def check_wall_clock_limits(
    *,
    batch_seconds: float | None = None,
    readback_seconds: float | None = None,
) -> list[str]:
    """
    Enforce batch/readback wall times when env caps are set.

    Measured in the orchestrator around ``BatchOrchestrator.run_batch`` (write+readback
    phases reported separately from ``BatchOrchestratorResult.timing_seconds``).
    """
    errors: list[str] = []
    batch_cap = os.environ.get("HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS")
    if batch_cap and batch_seconds is not None:
        cap = float(batch_cap)
        if batch_seconds > cap:
            errors.append(
                f"batch wall {batch_seconds:.1f}s > "
                f"HISTORICAL_IMPORT_MAX_BATCH_WALL_SECONDS={cap}"
            )
    read_cap = os.environ.get("HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS")
    if read_cap and readback_seconds is not None:
        cap = float(read_cap)
        if readback_seconds > cap:
            errors.append(
                f"readback wall {readback_seconds:.1f}s > "
                f"HISTORICAL_IMPORT_MAX_READBACK_WALL_SECONDS={cap}"
            )
    return errors
