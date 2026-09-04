"""Disk space monitoring for raw archive."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiskStatus:
    free_gb: float
    total_gb: float
    below_warn: bool
    below_min: bool


def check_disk(path: Path, *, warn_gb: float, min_gb: float) -> DiskStatus:
    target = path
    if not target.exists():
        target = path.parent if path.parent.exists() else Path(".")
    usage = shutil.disk_usage(target)
    free_gb = usage.free / (1024**3)
    total_gb = usage.total / (1024**3)
    return DiskStatus(
        free_gb=round(free_gb, 3),
        total_gb=round(total_gb, 3),
        below_warn=free_gb < warn_gb,
        below_min=free_gb < min_gb,
    )
