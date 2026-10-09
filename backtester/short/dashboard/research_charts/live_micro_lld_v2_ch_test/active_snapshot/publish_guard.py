"""Validate snapshot content before publish (beyond write_complete + member count)."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .extract import ActiveStateSnapshot, compare_active_sets
from .fingerprint import active_state_fingerprint
from .reader import ActiveSnapshotReader, IncompleteSnapshotError, SnapshotNotFoundError


class PublishGuardError(Exception):
    pass


class PublishConflictError(PublishGuardError):
    pass


@dataclass(frozen=True)
class LineIdentity:
    symbol: str
    timeframe: str
    pool_detail: int
    config_hash: str
    engine_version: str
    candle_load_start: datetime
    history_from_close: datetime
    history_policy: str = ""


def deterministic_publish_id(snapshot_id: str) -> str:
    raw = json.dumps({"snapshot_id": snapshot_id, "kind": "published"}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def validate_snapshot_against_state(
    reader: ActiveSnapshotReader,
    snapshot_id: str,
    expected: ActiveStateSnapshot,
    line: LineIdentity,
) -> dict[str, Any]:
    """Read complete snapshot from CH and verify every identity + content field."""
    try:
        meta = reader.load_meta(snapshot_id)
    except (SnapshotNotFoundError, IncompleteSnapshotError) as exc:
        raise PublishGuardError(str(exc)) from exc

    pool_ids, sides = reader.load_active_members(snapshot_id)
    recomputed_fp = active_state_fingerprint(pool_ids, sides)
    if recomputed_fp != meta["active_state_fingerprint"]:
        raise PublishGuardError("member-derived fingerprint != meta fingerprint")
    if recomputed_fp != expected.active_state_fingerprint:
        raise PublishGuardError("expected active_state_fingerprint mismatch")

    cmp = compare_active_sets(expected, pool_ids, sides)
    if not cmp["pass"]:
        raise PublishGuardError(f"active set mismatch: {cmp}")

    checks = {
        "symbol": (meta["symbol"], expected.symbol.upper()),
        "timeframe": (meta["timeframe"], expected.timeframe),
        "pool_detail": (meta["pool_detail"], expected.pool_detail),
        "config_hash": (meta["config_hash"], expected.config_hash),
        "engine_version": (meta["engine_version"], expected.engine_version),
        "engine_input_hash": (meta["engine_input_hash"], expected.engine_input_hash),
        "prefix_candle_fingerprint": (
            meta["prefix_candle_fingerprint"],
            expected.prefix_candle_fingerprint,
        ),
    }
    for name, (got, exp) in checks.items():
        if got != exp:
            raise PublishGuardError(f"{name} mismatch: meta={got!r} expected={exp!r}")

    dc_meta = meta["decision_close"]
    dc_exp = expected.decision_close
    if hasattr(dc_meta, "timestamp"):
        if dc_meta.replace(tzinfo=dc_exp.tzinfo) != dc_exp and dc_meta != dc_exp:
            # clickhouse may return naive utc
            from datetime import timezone

            if dc_meta.tzinfo is None:
                dc_meta = dc_meta.replace(tzinfo=timezone.utc)
            if dc_meta != dc_exp.astimezone(dc_meta.tzinfo):
                raise PublishGuardError(f"decision_close mismatch")

    line_checks = {
        "symbol": line.symbol.upper(),
        "timeframe": line.timeframe,
        "pool_detail": line.pool_detail,
        "config_hash": line.config_hash,
        "engine_version": line.engine_version,
    }
    for k, v in line_checks.items():
        if meta[k] != v:
            raise PublishGuardError(f"line identity {k} mismatch")

    cls_meta = meta["candle_load_start"]
    cls_line = line.candle_load_start
    if hasattr(cls_meta, "timestamp"):
        from datetime import timezone

        if cls_meta.tzinfo is None:
            cls_meta = cls_meta.replace(tzinfo=timezone.utc)
        if cls_meta != cls_line.astimezone(timezone.utc):
            raise PublishGuardError("candle_load_start mismatch")

    return {
        "snapshot_id": snapshot_id,
        "active_state_fingerprint": recomputed_fp,
        "active_pool_count": len(pool_ids),
        "validated": True,
    }
