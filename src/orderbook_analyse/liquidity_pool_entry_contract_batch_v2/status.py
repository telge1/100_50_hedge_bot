"""Atomic case status / checkpoint helpers for expansion batch V2."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from orderbook_analyse.liquidity_pool_entry_contract_batch_v2 import (
    RESULTS_DIR_REL,
    STATUS_FAILED_RETRYABLE,
    STATUS_MECHANICAL_COMPLETE,
    STATUS_PENDING,
    STATUS_RUNNING,
    STATUS_UNBLINDED,
    STALE_RUNNING_S,
)
from orderbook_analyse.liquidity_pool_entry_contract_batch_v2.hashes import payload_sha256


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, default=str)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, sort_keys=True, default=str) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def batch_root(repo_root: Path) -> Path:
    return repo_root / RESULTS_DIR_REL


def case_dir(repo_root: Path, case_id: str) -> Path:
    return batch_root(repo_root) / "cases" / case_id


def case_status_path(repo_root: Path, case_id: str) -> Path:
    return case_dir(repo_root, case_id) / "case_status.json"


def mechanical_payload_path(repo_root: Path, case_id: str) -> Path:
    return case_dir(repo_root, case_id) / "mechanical_verdict_pre_unblind.json"


def default_case_status(case_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": STATUS_PENDING,
        "updated_at_utc": _utc_now(),
        "mechanical_payload_sha256": None,
        "error": None,
        "started_at_utc": None,
        "finished_at_utc": None,
        "elapsed_s": None,
        "peak_rss_mb": None,
        "prefix_status": None,
        "worker_pid": None,
        "outcomes_read": False,
        "market_data_loaded": False,
    }


def read_case_status(repo_root: Path, case_id: str) -> dict[str, Any]:
    path = case_status_path(repo_root, case_id)
    if not path.is_file():
        return default_case_status(case_id)
    return json.loads(path.read_text(encoding="utf-8"))


def write_case_status(repo_root: Path, case_id: str, status: dict[str, Any]) -> None:
    status = dict(status)
    status["case_id"] = case_id
    status["updated_at_utc"] = _utc_now()
    atomic_write_json(case_status_path(repo_root, case_id), status)


def is_stale_running(status: dict[str, Any], *, now: datetime | None = None) -> bool:
    if status.get("status") != STATUS_RUNNING:
        return False
    started = status.get("started_at_utc")
    if not started:
        return True
    now = now or datetime.now(timezone.utc)
    age = (now - _utc_parse(started)).total_seconds()
    return age > STALE_RUNNING_S


def recover_stale_running(repo_root: Path, case_id: str) -> dict[str, Any]:
    st = read_case_status(repo_root, case_id)
    if is_stale_running(st):
        st["status"] = STATUS_FAILED_RETRYABLE
        st["error"] = "stale_RUNNING_recovered"
        st["worker_pid"] = None
        write_case_status(repo_root, case_id, st)
    return st


def mechanical_complete_valid(repo_root: Path, case_id: str) -> tuple[bool, str | None]:
    st = read_case_status(repo_root, case_id)
    if st.get("status") != STATUS_MECHANICAL_COMPLETE:
        return False, "status_not_complete"
    path = mechanical_payload_path(repo_root, case_id)
    if not path.is_file():
        return False, "payload_missing"
    marker = case_dir(repo_root, case_id) / "mechanical_complete.marker"
    if not marker.is_file():
        return False, "marker_missing"
    if any(case_dir(repo_root, case_id).glob("*.tmp")):
        return False, "tmp_artifacts_present"
    try:
        mech = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False, "payload_unreadable"
    stored = mech.get("mechanical_payload_sha256") or st.get("mechanical_payload_sha256")
    if not stored:
        return False, "payload_sha_missing"
    recomputed = payload_sha256(mech)
    if recomputed != stored:
        return False, "payload_sha_mismatch"
    if marker.read_text(encoding="utf-8").strip() != stored:
        return False, "marker_sha_mismatch"
    if st.get("mechanical_payload_sha256") and st["mechanical_payload_sha256"] != stored:
        return False, "status_sha_mismatch"
    return True, None


def count_mechanical_complete(repo_root: Path, case_ids: list[str]) -> int:
    n = 0
    for cid in case_ids:
        ok, _ = mechanical_complete_valid(repo_root, cid)
        if ok:
            n += 1
    return n


def build_batch_status(repo_root: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    case_rows = []
    for c in cases:
        cid = c["expansion_case_id"]
        st = recover_stale_running(repo_root, cid)
        if st.get("status") == STATUS_MECHANICAL_COMPLETE:
            ok, reason = mechanical_complete_valid(repo_root, cid)
            if not ok:
                st["status"] = STATUS_FAILED_RETRYABLE
                st["error"] = f"invalid_complete:{reason}"
                write_case_status(repo_root, cid, st)
        by_status[st["status"]] = by_status.get(st["status"], 0) + 1
        case_rows.append(
            {
                "case_id": cid,
                "pool_side": c.get("pool_side"),
                "status": st["status"],
                "mechanical_payload_sha256": st.get("mechanical_payload_sha256"),
                "error": st.get("error"),
            }
        )
    return {
        "updated_at_utc": _utc_now(),
        "counts": by_status,
        "mechanical_complete_count": by_status.get(STATUS_MECHANICAL_COMPLETE, 0),
        "unblinded_count": by_status.get(STATUS_UNBLINDED, 0),
        "cases": case_rows,
    }
