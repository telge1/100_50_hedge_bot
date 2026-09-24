from __future__ import annotations

import shutil
from pathlib import Path

from .config import DISK_RESERVE_BYTES, MIN_FREE_BYTES_51, PER_COIN_DISK_BYTES


def required_free_bytes(n_coins: int) -> int:
    n = max(0, int(n_coins))
    base = n * PER_COIN_DISK_BYTES + DISK_RESERVE_BYTES
    if n >= 51:
        return max(base, MIN_FREE_BYTES_51)
    return base


def free_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return int(usage.free)


def assert_disk(path: Path, n_coins: int) -> str | None:
    path.mkdir(parents=True, exist_ok=True)
    need = required_free_bytes(n_coins)
    if free_bytes(path) < need:
        return "INSUFFICIENT_DISK"
    return None
