"""Disjoint blocks, local publish files, and fail-closed merge. No ClickHouse."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..active_snapshot.runner import enumerate_decision_closes

FIRST_CLOSE = datetime(2026, 9, 30, 11, 20, tzinfo=timezone.utc)
CLOSE_COUNT = 9
WORKER_COUNT = 3
PHASE10_LINE = "phase10_DOGEUSDT_5m_PD10_ROLLING4M_7d_9e40e90b"


class PublishMergeError(RuntimeError):
    pass


def planned_closes() -> list[datetime]:
    return enumerate_decision_closes(FIRST_CLOSE, CLOSE_COUNT, "5m")


def disjoint_blocks(closes: list[datetime], workers: int = WORKER_COUNT) -> list[list[datetime]]:
    if workers < 1 or len(closes) % workers != 0:
        raise PublishMergeError("close count must divide evenly across workers")
    size = len(closes) // workers
    blocks = [closes[i * size : (i + 1) * size] for i in range(workers)]
    seen: set[str] = set()
    for block in blocks:
        for close in block:
            key = close.astimezone(timezone.utc).isoformat()
            if key in seen:
                raise PublishMergeError(f"duplicate close {key}")
            seen.add(key)
    if [c for block in blocks for c in block] != list(closes):
        raise PublishMergeError("blocks do not cover the planned closes in order")
    for left, right in zip(blocks, blocks[1:]):
        if left[-1] >= right[0]:
            raise PublishMergeError("worker blocks overlap")
    return blocks


def artifact_bytes(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")


def write_local_publish(directory: Path, record: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    close = datetime.fromisoformat(record["decision_close"])
    name = close.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    path = directory / name
    path.write_bytes(artifact_bytes(record))
    return path


def merge_worker_payloads(payloads: list[dict], expected: list[datetime]) -> dict:
    if len(payloads) != WORKER_COUNT:
        raise PublishMergeError(f"expected {WORKER_COUNT} workers, got {len(payloads)}")
    identities = []
    records = []
    for payload in payloads:
        if payload.get("status") != "PASS":
            raise PublishMergeError(payload.get("error") or "worker failed")
        identities.append((payload["snapshot_id"], payload["price_hash"], payload["revision_hash"]))
        if int(payload.get("clickhouse_candle_reads") or 0) != 0:
            raise PublishMergeError("worker read candles from ClickHouse")
        records.extend(payload["records"])
    if len(set(identities)) != 1:
        raise PublishMergeError("workers did not share one frozen input identity")
    keys = [row["decision_close"] for row in records]
    if len(keys) != len(set(keys)):
        raise PublishMergeError("duplicate decision close across workers")
    expected_keys = [c.astimezone(timezone.utc).isoformat() for c in expected]
    if sorted(keys) != sorted(expected_keys):
        missing = sorted(set(expected_keys) - set(keys))
        extra = sorted(set(keys) - set(expected_keys))
        raise PublishMergeError(f"close coverage missing={missing} extra={extra}")
    ordered = sorted(records, key=lambda row: row["decision_close"])
    digest = hashlib.sha256(b"".join(artifact_bytes(row) for row in ordered)).hexdigest()
    return {
        "records": ordered,
        "merge_digest": digest,
        "identity": {
            "snapshot_id": identities[0][0],
            "price_hash": identities[0][1],
            "revision_hash": identities[0][2],
        },
    }


def repeat_digest(records: list[dict]) -> str:
    ordered = sorted(records, key=lambda row: row["decision_close"])
    return hashlib.sha256(b"".join(artifact_bytes(row) for row in ordered)).hexdigest()


def tail_open_for_coverage(closes: list[datetime]) -> datetime:
    """Session fetch is inclusive of the decision-close unix second."""
    return max(closes).astimezone(timezone.utc)
