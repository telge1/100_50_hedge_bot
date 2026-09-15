"""Execution hold, dual live gates, and mid-run Silver-lock sentinel."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import FULL_RUN_BLOCK_REASON

EXPLICIT_EXECUTION_REQUIRED = "FULL_RUN_BLOCKED_EXPLICIT_EXECUTION_REQUIRED"
STOP_ACTIVE_SILVER_BUILDER_DURING_ANALYSIS = (
    "STOP_ACTIVE_SILVER_BUILDER_DURING_ANALYSIS"
)
STOP_SILVER_LOCK_TAMPERED = "STOP_SILVER_LOCK_TAMPERED"

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
    content_sha256: str | None = None


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


def _lock_content_sha(lock_path: Path) -> str | None:
    if not lock_path.exists():
        return None
    try:
        data = lock_path.read_bytes()
    except OSError:
        return None
    return hashlib.sha256(data).hexdigest()


def read_builder_lock(lock_path: Path = DEFAULT_SILVER_LOCK) -> LockProbe:
    digest = _lock_content_sha(lock_path)
    if not lock_path.exists():
        return LockProbe(
            lock_path=lock_path,
            exists=False,
            pid=None,
            pid_alive=False,
            raw=None,
            blocks_full_run=False,
            reason="",
            content_sha256=None,
        )
    try:
        raw = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return LockProbe(
            lock_path=lock_path,
            exists=True,
            pid=None,
            pid_alive=False,
            raw=None,
            blocks_full_run=True,
            reason=FULL_RUN_BLOCK_REASON + ":lock_unreadable",
            content_sha256=digest,
        )
    pid = raw.get("pid")
    try:
        pid_i = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_i = None
    alive = _pid_alive(pid_i)
    return LockProbe(
        lock_path=lock_path,
        exists=True,
        pid=pid_i,
        pid_alive=alive,
        raw=raw if isinstance(raw, dict) else None,
        blocks_full_run=bool(alive),
        reason=FULL_RUN_BLOCK_REASON if alive else "",
        content_sha256=digest,
    )


def assert_full_run_allowed(lock_path: Path = DEFAULT_SILVER_LOCK) -> LockProbe:
    probe = read_builder_lock(lock_path)
    if probe.blocks_full_run:
        raise RuntimeError(probe.reason or FULL_RUN_BLOCK_REASON)
    return probe


def assert_live_execution_allowed(
    *,
    execute_live: bool,
    lock_path: Path = DEFAULT_SILVER_LOCK,
) -> LockProbe:
    if not execute_live:
        raise RuntimeError(EXPLICIT_EXECUTION_REQUIRED)
    return assert_full_run_allowed(lock_path)


def assert_lock_untouched(lock_path: Path = DEFAULT_SILVER_LOCK) -> dict[str, Any]:
    probe = read_builder_lock(lock_path)
    return {
        "lock_path": str(lock_path),
        "exists": probe.exists,
        "pid": probe.pid,
        "pid_alive": probe.pid_alive,
        "blocks_full_run": probe.blocks_full_run,
        "reason": probe.reason,
        "content_sha256": probe.content_sha256,
    }


class ExecutionSentinel:
    """Read-only mid-run Silver-lock watcher. Never writes the lock file."""

    def __init__(self, lock_path: Path = DEFAULT_SILVER_LOCK):
        self.lock_path = Path(lock_path)
        start = read_builder_lock(self.lock_path)
        if start.blocks_full_run:
            raise RuntimeError(start.reason or FULL_RUN_BLOCK_REASON)
        self._start_exists = start.exists
        self._start_sha = start.content_sha256
        self._start_raw = start.raw
        self.checks: list[str] = []

    def check(self, stage: str) -> LockProbe:
        self.checks.append(stage)
        probe = read_builder_lock(self.lock_path)
        if probe.blocks_full_run:
            raise RuntimeError(
                f"{STOP_ACTIVE_SILVER_BUILDER_DURING_ANALYSIS}:stage={stage}"
            )
        # Lock must not be rewritten by anyone in a way that changes content
        # while analysis runs, unless it was absent and stays absent.
        if self._start_exists:
            if not probe.exists:
                raise RuntimeError(
                    f"{STOP_SILVER_LOCK_TAMPERED}:stage={stage}:lock_removed"
                )
            if probe.content_sha256 != self._start_sha:
                # Content change with dead pid still counts as tamper/race.
                raise RuntimeError(
                    f"{STOP_SILVER_LOCK_TAMPERED}:stage={stage}:content_changed"
                )
        elif probe.exists and not probe.blocks_full_run:
            # New inactive lock file appeared — treat as tamper/race signal.
            raise RuntimeError(
                f"{STOP_SILVER_LOCK_TAMPERED}:stage={stage}:lock_appeared"
            )
        return probe

    def to_dict(self) -> dict[str, Any]:
        return {
            "lock_path": str(self.lock_path),
            "start_exists": self._start_exists,
            "start_sha256": self._start_sha,
            "checks": list(self.checks),
        }
