"""Central atomic case reservation for concurrency<=2 batch workers."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    STATUS_FAILED_RETRYABLE,
    STATUS_MECHANICAL_COMPLETE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STALE_RUNNING_S,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.status import (
    append_jsonl,
    atomic_write_json,
    batch_root,
    case_status_path,
    is_stale_running,
    mechanical_complete_valid,
    read_case_status,
    write_case_status,
)

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore


class CoordinationError(RuntimeError):
    def __init__(self, verdict: str, detail: str = ""):
        self.verdict = verdict
        super().__init__(f"{verdict}: {detail}" if detail else verdict)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Reservation:
    case_id: str
    worker_id: str
    worker_pid: int
    reserved_at_utc: str


class BatchCoordinator:
    """File-lock + CAS reservation. Max concurrent RUNNING enforced by caller."""

    def __init__(self, repo_root: Path, *, max_concurrency: int = 2):
        if max_concurrency < 1 or max_concurrency > 2:
            raise CoordinationError(
                "PARALLEL_BATCH_COORDINATION_FAILURE",
                f"max_concurrency={max_concurrency} not in {{1,2}}",
            )
        self.repo_root = Path(repo_root)
        self.max_concurrency = max_concurrency
        self._stop = threading.Event()
        self._stop_reason: str | None = None
        self._lock_path = batch_root(self.repo_root) / ".batch_reservation.lock"
        self._stop_path = batch_root(self.repo_root) / ".batch_stop_flag.json"
        self._query_lock_path = batch_root(self.repo_root) / ".query_audit.lock"
        batch_root(self.repo_root).mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        self._query_lock_path.touch(exist_ok=True)

    def request_stop(self, reason: str) -> None:
        self._stop_reason = reason
        self._stop.set()
        atomic_write_json(
            self._stop_path,
            {"stop": True, "reason": reason, "at_utc": _utc_now()},
        )

    def stop_requested(self) -> bool:
        if self._stop.is_set():
            return True
        if self._stop_path.is_file():
            try:
                obj = json.loads(self._stop_path.read_text(encoding="utf-8"))
                if obj.get("stop"):
                    self._stop_reason = obj.get("reason")
                    self._stop.set()
                    return True
            except Exception as exc:
                raise CoordinationError(
                    "PARALLEL_BATCH_COORDINATION_FAILURE",
                    f"stop flag unreadable: {exc}",
                ) from exc
        return False

    def clear_stop(self) -> None:
        self._stop.clear()
        self._stop_reason = None
        if self._stop_path.is_file():
            self._stop_path.unlink()

    def _with_lock(self, lock_path: Path, fn):
        if fcntl is None:
            raise CoordinationError(
                "PARALLEL_BATCH_COORDINATION_FAILURE", "fcntl unavailable"
            )
        with lock_path.open("a+", encoding="utf-8") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                return fn()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def append_query_audit(self, obj: dict[str, Any]) -> None:
        path = batch_root(self.repo_root) / "query_audit.jsonl"

        def _append() -> None:
            append_jsonl(path, obj)

        self._with_lock(self._query_lock_path, _append)

    def count_running(self, case_ids: list[str]) -> int:
        n = 0
        for cid in case_ids:
            st = read_case_status(self.repo_root, cid)
            if st.get("status") == STATUS_RUNNING and not is_stale_running(st):
                n += 1
        return n

    def recover_stale_locked(self, case_id: str) -> dict[str, Any]:
        st = read_case_status(self.repo_root, case_id)
        if is_stale_running(st):
            st["status"] = STATUS_FAILED_RETRYABLE
            st["error"] = "stale_RUNNING_recovered"
            st["worker_pid"] = None
            st["worker_id"] = None
            write_case_status(self.repo_root, case_id, st)
        return st

    def try_reserve(
        self,
        case_ids: list[str],
        *,
        worker_id: str,
        worker_pid: int | None = None,
    ) -> Reservation | None:
        """Atomically reserve next eligible case. Returns None if none / stop / at cap."""
        pid = int(worker_pid or os.getpid())

        def _reserve() -> Reservation | None:
            if self.stop_requested():
                return None
            # Enforce concurrency against live RUNNING among sample
            if self.count_running(case_ids) >= self.max_concurrency:
                return None
            for cid in case_ids:
                ok_complete, _ = mechanical_complete_valid(self.repo_root, cid)
                if ok_complete:
                    continue
                st = self.recover_stale_locked(cid)
                status = st.get("status")
                if status == STATUS_RUNNING and not is_stale_running(st):
                    # owned by another worker
                    continue
                if status == STATUS_MECHANICAL_COMPLETE:
                    # invalid complete already demoted elsewhere; skip claim races
                    continue
                if status not in (STATUS_PENDING, STATUS_FAILED_RETRYABLE):
                    # FAILED_FINAL etc. — not auto-reclaimed here
                    if status == "FAILED_FINAL" and "FROZEN_INPUT_HASH_MISMATCH:forced" in str(
                        st.get("error") or ""
                    ):
                        # allow reclaim of unit-test pollution only via explicit reset
                        pass
                    else:
                        continue
                # CAS claim
                claim = dict(st)
                claim["status"] = STATUS_RUNNING
                claim["worker_id"] = worker_id
                claim["worker_pid"] = pid
                claim["started_at_utc"] = _utc_now()
                claim["error"] = None
                claim["outcomes_read"] = False
                claim["reservation_token"] = f"{worker_id}:{pid}:{cid}:{claim['started_at_utc']}"
                write_case_status(self.repo_root, cid, claim)
                # verify we still own it
                verify = read_case_status(self.repo_root, cid)
                if (
                    verify.get("status") == STATUS_RUNNING
                    and verify.get("worker_id") == worker_id
                    and verify.get("worker_pid") == pid
                ):
                    return Reservation(
                        case_id=cid,
                        worker_id=worker_id,
                        worker_pid=pid,
                        reserved_at_utc=claim["started_at_utc"],
                    )
                raise CoordinationError(
                    "PARALLEL_BATCH_COORDINATION_FAILURE",
                    f"CAS reservation lost for {cid}",
                )
            return None

        return self._with_lock(self._lock_path, _reserve)

    def mark_retryable_interrupted(self, case_id: str, *, worker_id: str) -> None:
        def _mark() -> None:
            st = read_case_status(self.repo_root, case_id)
            if st.get("status") != STATUS_RUNNING:
                return
            if st.get("worker_id") and st.get("worker_id") != worker_id:
                return
            st["status"] = STATUS_FAILED_RETRYABLE
            st["error"] = "interrupted_SIGINT_SIGTERM"
            st["worker_pid"] = None
            st["worker_id"] = None
            write_case_status(self.repo_root, case_id, st)

        self._with_lock(self._lock_path, _mark)

    def assert_owned(self, case_id: str, *, worker_id: str, worker_pid: int) -> None:
        st = read_case_status(self.repo_root, case_id)
        if st.get("status") != STATUS_RUNNING:
            raise CoordinationError(
                "PARALLEL_BATCH_COORDINATION_FAILURE",
                f"{case_id} not RUNNING under {worker_id}",
            )
        if st.get("worker_id") != worker_id or int(st.get("worker_pid") or -1) != int(worker_pid):
            raise CoordinationError(
                "PARALLEL_BATCH_COORDINATION_FAILURE",
                f"{case_id} ownership mismatch",
            )
