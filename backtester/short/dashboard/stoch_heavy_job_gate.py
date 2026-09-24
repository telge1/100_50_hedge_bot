"""Atomic single-flight gate for heavy historical dashboard jobs.

Candle-update and Frozen-research must not run at the same time.
Per-job ACTIVE.lock files remain; this gate is the race-safe cross lock.

Lock file is created with O_CREAT|O_EXCL. Stale locks are dropped only after
/proc checks; foreign PIDs are never killed.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stoch_universe_51.jsonio import read_json, write_json_atomic

OWNER_CANDLE_UPDATE = "CANDLE_UPDATE"
OWNER_FROZEN_RESEARCH = "FROZEN_RESEARCH"
OWNER_FROZEN_OUTCOME_EVALUATION = "FROZEN_OUTCOME_EVALUATION"
NEEDLES = {
    OWNER_CANDLE_UPDATE: "update_worker.py",
    OWNER_FROZEN_RESEARCH: "stoch_fade_research_jobs/worker.py",
    OWNER_FROZEN_OUTCOME_EVALUATION: "stoch_fade_research_evaluations/worker.py",
}

ERR_UPDATE_BLOCKS_FROZEN = "UPDATE_JOB_BLOCKS_FROZEN_RESEARCH"
ERR_FROZEN_BLOCKS_UPDATE = "FROZEN_JOB_BLOCKS_CANDLE_UPDATE"
ERR_BUSY = "HEAVY_JOB_RESOURCE_BUSY"
ERR_UPDATE_ALREADY = "UPDATE_JOB_ALREADY_RUNNING"
ERR_FROZEN_ALREADY = "FROZEN_JOB_ALREADY_RUNNING"
ERR_EVAL_ALREADY = "OUTCOME_EVAL_ALREADY_RUNNING"
ERR_UPDATE_BLOCKS_EVAL = "UPDATE_JOB_BLOCKS_OUTCOME_EVALUATION"
ERR_FROZEN_BLOCKS_EVAL = "FROZEN_JOB_BLOCKS_OUTCOME_EVALUATION"
ERR_EVAL_BLOCKS_UPDATE = "OUTCOME_EVAL_BLOCKS_CANDLE_UPDATE"
ERR_EVAL_BLOCKS_FROZEN = "OUTCOME_EVAL_BLOCKS_FROZEN_RESEARCH"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def gate_path(environ: dict | None = None) -> Path:
    env = environ if environ is not None else os.environ
    override = str(env.get("STOCH_HEAVY_JOB_GATE") or "").strip()
    if override:
        return Path(override)
    repo = Path(__file__).resolve().parent.parent
    return repo / "results" / "HEAVY_HISTORY.lock"


def _proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def worker_is_live(pid: int | None, job_id: str, owner_type: str) -> bool:
    if not pid_alive(pid):
        return False
    cmd = _proc_cmdline(int(pid)).replace("\\", "/")
    needle = NEEDLES.get(owner_type, "")
    return bool(needle) and needle in cmd and str(job_id) in cmd


def read_gate(environ: dict | None = None) -> dict[str, Any] | None:
    path = gate_path(environ)
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:  # noqa: BLE001
        return None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def reconcile_gate(environ: dict | None = None) -> dict[str, Any] | None:
    path = gate_path(environ)
    lock = read_gate(environ)
    if not lock:
        if path.exists():
            _unlink(path)
        return None
    job_id = str(lock.get("job_id") or "")
    owner = str(lock.get("owner_type") or "")
    pid = lock.get("pid")
    if worker_is_live(pid if isinstance(pid, int) else None, job_id, owner):
        return lock
    if not isinstance(pid, int) or pid <= 0:
        started = str(lock.get("started_at") or "")
        try:
            ts = datetime.fromisoformat(started.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:  # noqa: BLE001
            age = 9999
        if age < 60:
            return lock
    _unlink(path)
    return None


def _conflict_error(existing: dict[str, Any], wanted: str) -> str:
    owner = str(existing.get("owner_type") or "")
    if wanted == OWNER_FROZEN_RESEARCH:
        if owner == OWNER_CANDLE_UPDATE:
            return ERR_UPDATE_BLOCKS_FROZEN
        if owner == OWNER_FROZEN_RESEARCH:
            return ERR_FROZEN_ALREADY
        if owner == OWNER_FROZEN_OUTCOME_EVALUATION:
            return ERR_EVAL_BLOCKS_FROZEN
        return ERR_BUSY
    if wanted == OWNER_CANDLE_UPDATE:
        if owner == OWNER_FROZEN_RESEARCH:
            return ERR_FROZEN_BLOCKS_UPDATE
        if owner == OWNER_CANDLE_UPDATE:
            return ERR_UPDATE_ALREADY
        if owner == OWNER_FROZEN_OUTCOME_EVALUATION:
            return ERR_EVAL_BLOCKS_UPDATE
        return ERR_BUSY
    if wanted == OWNER_FROZEN_OUTCOME_EVALUATION:
        if owner == OWNER_CANDLE_UPDATE:
            return ERR_UPDATE_BLOCKS_EVAL
        if owner == OWNER_FROZEN_RESEARCH:
            return ERR_FROZEN_BLOCKS_EVAL
        if owner == OWNER_FROZEN_OUTCOME_EVALUATION:
            return ERR_EVAL_ALREADY
        return ERR_BUSY
    return ERR_BUSY


def _exclusive_create(path: Path, payload: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError:
        return False
    try:
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def try_acquire(
    owner_type: str,
    job_id: str,
    *,
    pid: int | None = None,
    environ: dict | None = None,
) -> tuple[bool, str | None, dict[str, Any] | None]:
    path = gate_path(environ)
    existing = reconcile_gate(environ)
    if existing:
        return False, _conflict_error(existing, owner_type), existing
    payload = {
        "owner_type": owner_type,
        "job_id": job_id,
        "pid": pid,
        "started_at": _utcnow_iso(),
        "worker_cmdline_must_contain": [NEEDLES[owner_type], job_id],
    }
    if _exclusive_create(path, payload):
        return True, None, payload
    existing = reconcile_gate(environ) or read_gate(environ) or {}
    return False, _conflict_error(existing, owner_type) if existing else ERR_BUSY, existing or None


def refresh_pid(job_id: str, pid: int, *, environ: dict | None = None) -> None:
    path = gate_path(environ)
    lock = read_gate(environ)
    if not lock or str(lock.get("job_id")) != str(job_id):
        return
    lock["pid"] = pid
    write_json_atomic(path, lock)


def release(job_id: str, *, environ: dict | None = None) -> None:
    path = gate_path(environ)
    lock = read_gate(environ)
    if not lock:
        _unlink(path)
        return
    if str(lock.get("job_id")) != str(job_id):
        return
    _unlink(path)
