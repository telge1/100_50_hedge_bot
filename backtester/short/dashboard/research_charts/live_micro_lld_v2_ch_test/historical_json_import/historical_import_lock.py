"""
Exclusive historical batch writer lock (single-host ``flock``).

**Scope:** Only writers on the **same machine** sharing the lock file path.
Does **not** protect against concurrent imports on other hosts — use one
designated import host or add distributed coordination later.

Lock is held for the full critical section (conflict checks → write → publish).
Process exit releases the kernel flock (no orphaned lock after crash).
"""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_LOCK_PATH = Path(
    os.environ.get(
        "HISTORICAL_JSON_IMPORT_LOCK_FILE",
        "/tmp/historical_lmv2_json_import.flock",
    )
)

_LOCK_ENV = "HISTORICAL_JSON_IMPORT_LOCK_DISABLE"


class HistoricalImportLockError(RuntimeError):
    """Another import holds the lock or lock could not be acquired."""


@contextmanager
def historical_import_write_lock(
    lock_path: Path | None = None,
    *,
    blocking: bool = True,
) -> Iterator[None]:
    """
    Acquire exclusive ``flock`` on ``lock_path``.

    When ``blocking=False``, raise ``HistoricalImportLockError`` if busy.
    """
    if os.environ.get(_LOCK_ENV) == "1":
        yield
        return
    path = lock_path or DEFAULT_LOCK_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            raise HistoricalImportLockError(
                f"historical import lock busy: {path} (another writer active on this host)"
            ) from None
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
