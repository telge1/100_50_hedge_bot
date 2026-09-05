"""Run-state paths for public-trade rematerialization (separate from full-history)."""

from __future__ import annotations

import fcntl
import os
import signal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO

from .atomic_json import atomic_write_json, read_json, read_json_lenient
from .contracts import sanitize_json

REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_STATE_DIR = REPO_ROOT / "run" / "btc_doge_trade_rematerialization"
LOG_PATH = REPO_ROOT / "logs" / "btc_doge_trade_rematerialization.log"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_run_dirs() -> None:
    RUN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


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
        return int(stat_path.read_text(encoding="utf-8").split()[21])
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
    if not runner_owner_path().is_file():
        return {}
    try:
        return read_json(runner_owner_path())
    except Exception:  # noqa: BLE001
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


class RunnerLock:
    """Exclusive flock held for the lifetime of a rematerialization runner."""

    def __init__(self) -> None:
        self._fh: IO[str] | None = None
        self.runner_pid: int | None = None

    def acquire(self, *, launcher_pid: int | None = None, force: bool = False) -> dict[str, Any]:
        ensure_run_dirs()
        existing = read_runner_pid()
        if existing and _owner_matches(existing) and not force:
            return {"acquired": False, "reason": "ALREADY_RUNNING", "runner_pid": existing}

        self._fh = runner_lock_path().open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._fh.close()
            self._fh = None
            return {
                "acquired": False,
                "reason": "LOCK_HELD",
                "runner_pid": read_runner_pid(),
            }

        runner_pid = os.getpid()
        self.runner_pid = runner_pid
        owner = sanitize_json(
            {
                "runner_pid": runner_pid,
                "launcher_pid": launcher_pid,
                "runner_start_boot": _process_start_time(runner_pid),
                "started_at": _utc_now(),
            }
        )
        runner_pid_path().write_text(str(runner_pid), encoding="utf-8")
        if launcher_pid is not None:
            launcher_pid_path().write_text(str(launcher_pid), encoding="utf-8")
        atomic_write_json(runner_owner_path(), owner)
        write_heartbeat({"status": "STARTING", "runner_pid": runner_pid, "launcher_pid": launcher_pid})
        return {"acquired": True, "runner_pid": runner_pid, "launcher_pid": launcher_pid}

    def release(self) -> None:
        current = self.runner_pid or os.getpid()
        owner = read_runner_owner()
        if owner and int(owner.get("runner_pid", current)) != current:
            return
        for path in (runner_pid_path(),):
            if path.is_file():
                try:
                    if int(path.read_text().strip()) == current:
                        path.unlink(missing_ok=True)
                except ValueError:
                    path.unlink(missing_ok=True)
        if runner_owner_path().is_file() and (not owner or int(owner.get("runner_pid", current)) == current):
            runner_owner_path().unlink(missing_ok=True)
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None


def write_heartbeat(payload: dict[str, Any]) -> None:
    body = sanitize_json({"updated_at": _utc_now(), **payload})
    atomic_write_json(heartbeat_path(), body)


def read_heartbeat() -> dict[str, Any]:
    try:
        return read_json(heartbeat_path(), default={})
    except Exception:  # noqa: BLE001
        payload, _ = read_json_lenient(heartbeat_path(), default={})
        return payload


def write_progress(payload: dict[str, Any]) -> None:
    atomic_write_json(progress_path(), sanitize_json({"updated_at": _utc_now(), **payload}))


def read_progress() -> dict[str, Any]:
    try:
        return read_json(progress_path(), default={})
    except Exception:  # noqa: BLE001
        payload, corrupted = read_json_lenient(progress_path(), default={})
        if corrupted:
            payload["_corrupted_source"] = True
        return payload


def update_file_watermark(key: str, payload: dict[str, Any]) -> None:
    watermarks = read_json(watermarks_path(), default={})
    watermarks[key] = sanitize_json({**payload, "updated_at": _utc_now()})
    atomic_write_json(watermarks_path(), watermarks)


def status_snapshot() -> dict[str, Any]:
    runner_pid = read_runner_pid()
    return sanitize_json(
        {
            "runner_pid": runner_pid,
            "launcher_pid": (
                int(launcher_pid_path().read_text().strip())
                if launcher_pid_path().is_file()
                else None
            ),
            "runner_pid_alive": _owner_matches(runner_pid) if runner_pid else False,
            "runner_lock_exists": runner_lock_path().is_file(),
            "runner_owner": read_runner_owner(),
            "heartbeat": read_heartbeat(),
            "progress": read_progress(),
            "file_watermarks": read_json(watermarks_path(), default={}),
        }
    )


def request_stop() -> dict[str, Any]:
    pid = read_runner_pid()
    if not pid or not _owner_matches(pid):
        return {"stopped": True, "reason": "NOT_RUNNING"}
    os.kill(pid, signal.SIGTERM)
    return {"stopped": False, "reason": "SIGTERM_SENT", "runner_pid": pid}
