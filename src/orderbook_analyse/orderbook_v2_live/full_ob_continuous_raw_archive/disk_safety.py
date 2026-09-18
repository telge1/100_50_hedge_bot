"""Disk safety classification. RED is fatal for this archive."""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class DiskState(str, Enum):
    GREEN = "GREEN"
    AMBER = "AMBER"
    RED = "RED"


@dataclass(frozen=True)
class DiskSafetyStatus:
    state: DiskState
    free_gb: float
    free_percent: float
    reason: str = ""


class DiskSafetyError(RuntimeError):
    pass


def classify_disk(
    path: Path,
    *,
    warn_gb: float = 20.0,
    red_gb: float = 5.0,
    warn_percent: float = 10.0,
    red_percent: float = 2.0,
) -> DiskSafetyStatus:
    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = os.statvfs(target)
    free = usage.f_bavail * usage.f_frsize
    total = usage.f_blocks * usage.f_frsize
    free_gb = free / (1024**3)
    free_percent = 100.0 if total <= 0 else (free / total) * 100.0
    if free_gb <= red_gb or free_percent <= red_percent:
        state = DiskState.RED
        reason = "free_space_red"
    elif free_gb <= warn_gb or free_percent <= warn_percent:
        state = DiskState.AMBER
        reason = "free_space_amber"
    else:
        state = DiskState.GREEN
        reason = ""
    return DiskSafetyStatus(state, free_gb, free_percent, reason)


def classify_os_error(exc: BaseException) -> str | None:
    if isinstance(exc, OSError):
        if exc.errno == errno.ENOSPC:
            return "ENOSPC"
        if exc.errno == errno.EROFS:
            return "READ_ONLY_FILESYSTEM"
        if exc.errno in {errno.EACCES, errno.EPERM}:
            return "PERMISSION_DENIED"
    return None


def require_not_red(path: Path, **thresholds: float) -> DiskSafetyStatus:
    try:
        status = classify_disk(path, **thresholds)
    except OSError as exc:
        reason = classify_os_error(exc) or f"DISK_CHECK:{type(exc).__name__}"
        raise DiskSafetyError(reason) from exc
    if status.state is DiskState.RED:
        raise DiskSafetyError(status.reason)
    return status
