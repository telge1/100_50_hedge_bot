"""Execution hold: block live Bronze/full runs while Silver builder lock is active."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import FULL_RUN_BLOCK_REASON

DEFAULT_SILVER_LOCK = Path(
    "/home/telgenbuescher/projects/orderbook_analyse_ch_research_v1/"
    "obfull_research_engine/runs/silver_full_build_v1_3/build.lock"
)


@dataclass(frozen=True)
class LockProbe:
    lock_path: Path
    exists: bool
    pid: int | None
    pid_alive: bool
    raw: dict[str, Any] | None
    blocks_full_run: bool
    reason: str


def _pid_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_builder_lock(lock_path: Path = DEFAULT_SILVER_LOCK) -> LockProbe:
    if not lock_path.exists():
        return LockProbe(
            lock_path=lock_path,
            exists=False,
            pid=None,
            pid_alive=False,
            raw=None,
            blocks_full_run=False,
            reason="",
        )
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Fail closed: unreadable lock still blocks full run
        return LockProbe(
            lock_path=lock_path,
            exists=True,
            pid=None,
            pid_alive=False,
            raw=None,
            blocks_full_run=True,
            reason=FULL_RUN_BLOCK_REASON + ":lock_unreadable",
        )
    pid = raw.get("pid")
    try:
        pid_i = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_i = None
    alive = _pid_alive(pid_i)
    blocks = bool(alive)
    return LockProbe(
        lock_path=lock_path,
        exists=True,
        pid=pid_i,
        pid_alive=alive,
        raw=raw if isinstance(raw, dict) else None,
        blocks_full_run=blocks,
        reason=FULL_RUN_BLOCK_REASON if blocks else "",
    )


def assert_full_run_allowed(lock_path: Path = DEFAULT_SILVER_LOCK) -> LockProbe:
    probe = read_builder_lock(lock_path)
    if probe.blocks_full_run:
        raise RuntimeError(probe.reason or FULL_RUN_BLOCK_REASON)
    return probe


def assert_lock_untouched(lock_path: Path = DEFAULT_SILVER_LOCK) -> dict[str, Any]:
    """Read-only probe; never create/modify the lock file."""
    probe = read_builder_lock(lock_path)
    return {
        "lock_path": str(lock_path),
        "exists": probe.exists,
        "pid": probe.pid,
        "pid_alive": probe.pid_alive,
        "blocks_full_run": probe.blocks_full_run,
        "reason": probe.reason,
    }
