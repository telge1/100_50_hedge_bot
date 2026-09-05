"""Lock, PID, heartbeat and progress state for resumable backfill."""

from __future__ import annotations

import json
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .atomic_json import atomic_write_json, read_json, read_json_lenient
from .contracts import sanitize_json
from .full_history_contracts import RUN_STATE_DIR

_TERMINAL_HEARTBEAT: set[str] = set()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_run_dirs() -> None:
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    (RUN_STATE_DIR.parents[1] / "logs").mkdir(parents=True, exist_ok=True)


def runner_lock_path() -> Path:
    return RUN_STATE_DIR / "runner.lock"


def runner_pid_path() -> Path:
    return RUN_STATE_DIR / "runner.pid"


def launcher_pid_path() -> Path:
    return RUN_STATE_DIR / "launcher.pid"


def runner_owner_path() -> Path:
    return RUN_STATE_DIR / "runner_owner.json"


def heartbeat_path() -> Path:
    return RUN_STATE_DIR / "heartbeat.json"


def progress_path() -> Path:
    return RUN_STATE_DIR / "progress.json"


def watermarks_path() -> Path:
    return RUN_STATE_DIR / "watermarks.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_start_time(pid: int) -> int | None:
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.is_file():
        return None
    try:
        parts = stat_path.read_text(encoding="utf-8").split()
        return int(parts[21])
    except (IndexError, ValueError, OSError):
        return None


def read_runner_pid() -> int | None:
    path = runner_pid_path()
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def read_runner_owner() -> dict[str, Any]:
    path = runner_owner_path()
    if not path.is_file():
        return {}
    try:
        return read_json(path)
    except json.JSONDecodeError:
        return {}


def _owner_matches(pid: int) -> bool:
    owner = read_runner_owner()
    if not owner:
        return _pid_alive(pid)
    if int(owner.get("runner_pid", -1)) != pid:
        return False
    boot = owner.get("runner_start_boot")
    if boot is not None:
        current = _process_start_time(pid)
        if current is not None and int(boot) != current:
            return False
    return _pid_alive(pid)


def acquire_runner_lock(*, launcher_pid: int | None = None, force: bool = False) -> dict[str, Any]:
    ensure_run_dirs()
    existing = read_runner_pid()
    if existing and _owner_matches(existing) and not force:
        return {"acquired": False, "reason": "ALREADY_RUNNING", "runner_pid": existing}
    runner_pid = os.getpid()
    owner = sanitize_json(
        {
            "runner_pid": runner_pid,
            "launcher_pid": launcher_pid,
            "runner_start_boot": _process_start_time(runner_pid),
            "started_at": _utc_now(),
        }
    )
    runner_lock_path().write_text(str(runner_pid), encoding="utf-8")
    runner_pid_path().write_text(str(runner_pid), encoding="utf-8")
    if launcher_pid is not None:
        launcher_pid_path().write_text(str(launcher_pid), encoding="utf-8")
    atomic_write_json(runner_owner_path(), owner)
    write_heartbeat({"status": "STARTING", "runner_pid": runner_pid, "launcher_pid": launcher_pid})
    return {"acquired": True, "runner_pid": runner_pid, "launcher_pid": launcher_pid}


def release_runner_lock(*, runner_pid: int | None = None) -> None:
    owner = read_runner_owner()
    current = runner_pid or os.getpid()
    if owner and int(owner.get("runner_pid", current)) != current:
        return
    for path in (runner_lock_path(), runner_pid_path()):
        if path.is_file():
            path.unlink(missing_ok=True)
    if runner_owner_path().is_file() and (not owner or int(owner.get("runner_pid", current)) == current):
        runner_owner_path().unlink(missing_ok=True)


def write_heartbeat(payload: dict[str, Any]) -> None:
    status = str(payload.get("status", ""))
    if status in _TERMINAL_HEARTBEAT and heartbeat_path().is_file():
        existing = read_json(heartbeat_path(), default={})
        if existing.get("status") in _TERMINAL_HEARTBEAT and status != existing.get("status"):
            if status == "COMPLETED" and existing.get("status") == "FAILED":
                return
    body = sanitize_json({"updated_at": _utc_now(), **payload})
    atomic_write_json(heartbeat_path(), body)
    if status in {"FAILED", "COMPLETED", "STOPPED"}:
        _TERMINAL_HEARTBEAT.add(status)


def read_heartbeat() -> dict[str, Any]:
    try:
        return read_json(heartbeat_path(), default={})
    except json.JSONDecodeError:
        payload, _ = read_json_lenient(heartbeat_path(), default={})
        return payload


def write_progress(payload: dict[str, Any]) -> None:
    body = sanitize_json({"updated_at": _utc_now(), **payload})
    atomic_write_json(progress_path(), body)


def read_progress() -> dict[str, Any]:
    try:
        return read_json(progress_path(), default={})
    except json.JSONDecodeError:
        payload, corrupted = read_json_lenient(progress_path(), default={})
        if corrupted:
            payload["_corrupted_source"] = True
        return payload


def update_watermark(key: str, payload: dict[str, Any]) -> None:
    watermarks = read_json(watermarks_path(), default={})
    watermarks[key] = sanitize_json({**payload, "updated_at": _utc_now()})
    atomic_write_json(watermarks_path(), watermarks)


def read_watermarks() -> dict[str, Any]:
    return read_json(watermarks_path(), default={})


def mark_failed(
    *,
    error: Exception,
    failed_modality: str = "",
    failed_segment: str = "",
    last_safe_watermark: dict[str, Any] | None = None,
) -> None:
    write_heartbeat(
        sanitize_json(
            {
                "status": "FAILED",
                "error_type": type(error).__name__,
                "error_message": str(error)[:1000],
                "failed_modality": failed_modality,
                "failed_segment": failed_segment,
                "failed_at": _utc_now(),
                "runner_pid": os.getpid(),
                "last_safe_watermark": last_safe_watermark or {},
            }
        )
    )


def request_stop() -> dict[str, Any]:
    pid = read_runner_pid()
    if not pid or not _owner_matches(pid):
        return {"stopped": True, "reason": "NOT_RUNNING"}
    os.kill(pid, signal.SIGTERM)
    return {"stopped": False, "reason": "SIGTERM_SENT", "runner_pid": pid}


def status_snapshot() -> dict[str, Any]:
    runner_pid = read_runner_pid()
    owner = read_runner_owner()
    return sanitize_json(
        {
            "runner_pid": runner_pid,
            "launcher_pid": int(launcher_pid_path().read_text().strip()) if launcher_pid_path().is_file() else None,
            "runner_pid_alive": _owner_matches(runner_pid) if runner_pid else False,
            "runner_lock_exists": runner_lock_path().is_file(),
            "runner_owner": owner,
            "heartbeat": read_heartbeat(),
            "progress": read_progress(),
            "watermarks": read_watermarks(),
        }
    )


# Backward-compatible aliases
acquire_lock = acquire_runner_lock
release_lock = release_runner_lock
read_pid = read_runner_pid
pid_path = runner_pid_path
lock_path = runner_lock_path
