"""analysis_run_manifest_v1 — atomic stage tracking."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ORCHESTRATOR_CONTRACT, ORCHESTRATOR_VERSION, STAGES
from .run_key import atomic_write_json


def _now_z() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_manifest(
    *,
    symbol: str,
    start_z: str,
    end_z: str,
    run_key: str,
    run_dir: Path,
    config_hashes: dict[str, str],
    stage_order: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    order = list(stage_order) if stage_order is not None else list(STAGES)
    stages = {
        name: {
            "stage_name": name,
            "status": "PENDING",
            "started_at": None,
            "finished_at": None,
            "input_paths": [],
            "input_hashes": {},
            "output_paths": [],
            "output_hashes": {},
            "schema_version": None,
            "config_hash": None,
            "row_count": None,
            "error": None,
            "reused": False,
            "duration_seconds": None,
        }
        for name in order
    }
    return {
        "schema_version": "analysis_run_manifest_v1",
        "contract_version": ORCHESTRATOR_CONTRACT,
        "orchestrator_version": ORCHESTRATOR_VERSION,
        "symbol": symbol,
        "start": start_z,
        "end": end_z,
        "run_key": run_key,
        "run_dir": str(run_dir),
        "status": "PENDING",
        "verdict": None,
        "created_at": _now_z(),
        "updated_at": _now_z(),
        "config_hashes": config_hashes,
        "stages": stages,
        "stage_order": order,
    }


def load_manifest(path: Path) -> dict[str, Any] | None:
    import json

    if not path.exists() or path.name.endswith(".tmp"):
        return None
    # Reject incomplete tmp sibling
    if path.with_suffix(path.suffix + ".tmp").exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    manifest = deepcopy(manifest)
    manifest["updated_at"] = _now_z()
    atomic_write_json(path, manifest)


def mark_stage_running(manifest: dict[str, Any], stage: str) -> None:
    st = manifest["stages"][stage]
    st["status"] = "RUNNING"
    st["started_at"] = _now_z()
    st["error"] = None
    st["finished_at"] = None
    manifest["status"] = "RUNNING"


def mark_stage_complete(
    manifest: dict[str, Any],
    stage: str,
    *,
    output_paths: list[str] | None = None,
    output_hashes: dict[str, str] | None = None,
    input_paths: list[str] | None = None,
    input_hashes: dict[str, str] | None = None,
    schema_version: str | None = None,
    config_hash: str | None = None,
    row_count: int | None = None,
    reused: bool = False,
    duration_seconds: float | None = None,
    status: str = "COMPLETE",
) -> None:
    st = manifest["stages"][stage]
    st["status"] = status
    st["finished_at"] = _now_z()
    st["reused"] = reused
    if output_paths is not None:
        st["output_paths"] = output_paths
    if output_hashes is not None:
        st["output_hashes"] = output_hashes
    if input_paths is not None:
        st["input_paths"] = input_paths
    if input_hashes is not None:
        st["input_hashes"] = input_hashes
    if schema_version is not None:
        st["schema_version"] = schema_version
    if config_hash is not None:
        st["config_hash"] = config_hash
    if row_count is not None:
        st["row_count"] = row_count
    if duration_seconds is not None:
        st["duration_seconds"] = duration_seconds
    st["error"] = None


def mark_stage_failed(manifest: dict[str, Any], stage: str, error: str) -> None:
    st = manifest["stages"][stage]
    st["status"] = "FAILED"
    st["finished_at"] = _now_z()
    st["error"] = error
    manifest["status"] = "FAILED"


def mark_stage_blocked(manifest: dict[str, Any], stage: str, error: str) -> None:
    st = manifest["stages"][stage]
    st["status"] = "BLOCKED_COVERAGE"
    st["finished_at"] = _now_z()
    st["error"] = error
    st["started_at"] = st.get("started_at") or _now_z()
    manifest["status"] = "BLOCKED_COVERAGE"


def validate_stage_outputs(stage_rec: dict[str, Any], *, file_sha256) -> bool:
    """Return True if recorded output hashes still match on-disk files (no .tmp)."""
    if stage_rec.get("status") not in {"COMPLETE", "SKIPPED_REUSED"}:
        return False
    hashes = stage_rec.get("output_hashes") or {}
    if not hashes:
        return False
    for path_s, expected in hashes.items():
        p = Path(path_s)
        if not p.exists() or str(p).endswith(".tmp") or p.name.endswith(".tmp"):
            return False
        if (p.parent / (p.name + ".tmp")).exists():
            return False
        got = file_sha256(p)
        if got != expected:
            return False
    return True
