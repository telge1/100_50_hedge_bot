"""Atomic PID file + exclusive flock. Prevents a second collector instance."""

from __future__ import annotations

import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path

COLLECTOR_MODULES = frozenset(
    {
        "orderbook_analyse.oi_liquidation_collector",
        "orderbook_analyse.oi_liquidation_collector.collector",
    }
)


def cmdline_is_collector_python(cmdline: str, exe_name: str) -> bool:
    """True only for the real Python collector, not bash wrappers or backfill."""
    exe = exe_name.lower()
    if "python" not in exe:
        return False
    if "bash" in exe:
        return False
    parts = cmdline.split()
    if not parts:
        return False
    if Path(parts[0]).name in {"bash", "sh"}:
        return False
    if "-m" not in parts:
        return False
    idx = parts.index("-m")
    mod = parts[idx + 1] if idx + 1 < len(parts) else ""
    return mod in COLLECTOR_MODULES


def read_proc_identity(pid: int) -> tuple[str, str] | None:
    try:
        exe = os.readlink(f"/proc/{pid}/exe")
        raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
        return exe, raw
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return None


def pid_is_live_collector(pid: int) -> bool:
    ident = read_proc_identity(pid)
    if ident is None:
        return False
    exe, cmdline = ident
    return cmdline_is_collector_python(cmdline, Path(exe).name)


class SingleInstanceLock:
    def __init__(self, lock_path: Path, pid_path: Path) -> None:
        self.lock_path = lock_path
        self.pid_path = pid_path
        self._fd: int | None = None

    def _archive_stale_pid(self) -> None:
        if not self.pid_path.is_file():
            return
        try:
            raw = self.pid_path.read_text().strip()
            pid = int(raw)
        except (OSError, ValueError):
            return
        if pid_is_live_collector(pid):
            return
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        dest = self.pid_path.with_name(self.pid_path.name + f".stale.{ts}")
        try:
            self.pid_path.replace(dest)
        except OSError:
            pass

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.pid_path.parent.mkdir(parents=True, exist_ok=True)
        if self.pid_path.is_file():
            try:
                other = int(self.pid_path.read_text().strip())
            except (OSError, ValueError):
                other = None
            if other is not None and other != os.getpid() and pid_is_live_collector(other):
                raise RuntimeError(f"another oi/liquidation collector is pid {other}")
            if other is None or not pid_is_live_collector(other):
                self._archive_stale_pid()
        self._fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"another oi/liquidation collector holds {self.lock_path}") from exc
        os.ftruncate(self._fd, 0)
        os.write(self._fd, str(os.getpid()).encode())
        os.fsync(self._fd)
        tmp = self.pid_path.with_suffix(".pid.tmp")
        tmp.write_text(str(os.getpid()) + "\n")
        tmp.replace(self.pid_path)

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        try:
            if self.pid_path.is_file() and self.pid_path.read_text().strip() == str(os.getpid()):
                self.pid_path.unlink()
        except OSError:
            pass
