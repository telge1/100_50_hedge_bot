"""Process lock via PID file (validate command before kill)."""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def read_pid(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmdline_of(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def acquire_pid_file(path: Path, *, expected_token: str) -> None:
    """Write PID if no *other* live process with expected_token in cmdline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = read_pid(path)
    if existing and pid_alive(existing):
        if existing == os.getpid():
            # start script may have written our pid already
            return
        cmd = cmdline_of(existing)
        if expected_token in cmd:
            raise RuntimeError(f"already running pid={existing} cmd={cmd}")
        # stale or unrelated — do not kill; refuse if alive unrelated with same pid file
        raise RuntimeError(
            f"pid file {path} points to live pid={existing} without token {expected_token!r}: {cmd}"
        )
    path.write_text(str(os.getpid()) + "\n", encoding="utf-8")


def release_pid_file(path: Path) -> None:
    pid = read_pid(path)
    if pid == os.getpid() and path.exists():
        path.unlink(missing_ok=True)


def stop_pid_file(path: Path, *, expected_token: str, timeout_s: float = 20.0) -> str:
    pid = read_pid(path)
    if pid is None:
        return "no_pid"
    if not pid_alive(pid):
        path.unlink(missing_ok=True)
        return "stale_pid_removed"
    cmd = cmdline_of(pid)
    if expected_token not in cmd:
        return f"refused_foreign_process pid={pid} cmd={cmd}"
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not pid_alive(pid):
            path.unlink(missing_ok=True)
            return f"stopped_term pid={pid}"
        time.sleep(0.2)
    os.kill(pid, signal.SIGKILL)
    path.unlink(missing_ok=True)
    return f"stopped_kill pid={pid}"
