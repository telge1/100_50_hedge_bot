"""In-memory CH row plans (no database I/O)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..active_snapshot.publish_guard import deterministic_publish_id
from ..active_snapshot.extract import ActiveStateSnapshot


@dataclass(frozen=True)
class SnapshotWritePlan:
    snapshot_id: str
    test_run_id: str
    meta_row: tuple[Any, ...]
    member_rows: list[tuple[Any, ...]]
    catalog_rows: list[tuple[Any, ...]]
    publish_row: tuple[Any, ...]
    provenance: dict[str, str]


def _meta_tuple(state: ActiveStateSnapshot, snapshot_id: str, test_run_id: str) -> tuple[Any, ...]:
    return (
        snapshot_id,
        uuid.UUID(test_run_id),
        state.symbol,
        state.timeframe,
        state.pool_detail,
        state.config_hash,
        state.engine_version,
        state.engine_input_hash,
        state.candle_load_start,
        state.history_from_close,
        state.decision_close,
        state.prefix_candle_count,
        state.prefix_candle_fingerprint,
        state.known_pool_count,
        state.active_pool_count,
        state.active_upper_count,
        state.active_lower_count,
        state.active_state_fingerprint,
        1,
    )


def _member_tuples(snapshot_id: str, state: ActiveStateSnapshot) -> list[tuple[Any, ...]]:
    return [
        (snapshot_id, pid, side, idx)
        for idx, (pid, side) in enumerate(zip(state.active_pool_ids, state.active_sides))
    ]


def _catalog_tuples(catalog_rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            r["snapshot_id"],
            r["pipeline_line_id"],
            r["symbol"],
            r["timeframe"],
            r["pool_detail"],
            r["decision_close"],
            r["history_policy"],
            r["pool_id"],
            r["pool_type"],
            r["side"],
            r["top_price"],
            r["bottom_price"],
            r["strength"],
            r["strength_class"],
            r["known_at"],
            r["break_at"],
            r["reclaim_level"],
            r["candidate_created_at"],
            r["confirmed_at"],
            r["invalidated_at"],
            r["created_index"],
            r["row_fingerprint"],
        )
        for r in catalog_rows
    ]


def _publish_tuple(
    state: ActiveStateSnapshot,
    *,
    snapshot_id: str,
    pipeline_line_id: str,
) -> tuple[Any, ...]:
    pub_id = deterministic_publish_id(snapshot_id)
    return (
        pub_id,
        pipeline_line_id,
        snapshot_id,
        state.symbol,
        state.timeframe,
        state.pool_detail,
        state.config_hash,
        state.engine_version,
        state.engine_input_hash,
        state.candle_load_start,
        state.history_from_close,
        state.decision_close,
        state.prefix_candle_fingerprint,
        state.active_state_fingerprint,
        1,
    )


def build_write_plan(
    bundle: dict[str, Any],
    *,
    pipeline_line_id: str,
    test_run_id: str,
) -> SnapshotWritePlan:
    state: ActiveStateSnapshot = bundle["state"]
    sid = bundle["snapshot_id"]
    catalog = bundle["catalog_rows"]
    return SnapshotWritePlan(
        snapshot_id=sid,
        test_run_id=test_run_id,
        meta_row=_meta_tuple(state, sid, test_run_id),
        member_rows=_member_tuples(sid, state),
        catalog_rows=_catalog_tuples(catalog),
        publish_row=_publish_tuple(state, snapshot_id=sid, pipeline_line_id=pipeline_line_id),
        provenance={
            "relative_dest": str(bundle.get("relative_dest") or ""),
            "frozen_input_id": str(bundle.get("frozen_input_id") or ""),
            "frozen_price_hash": str(bundle.get("frozen_price_hash") or ""),
            "frozen_revision_hash": str(bundle.get("frozen_revision_hash") or ""),
            "source_path": str(bundle.get("path") or ""),
        },
    )


def summarize_plans(plans: list[SnapshotWritePlan]) -> dict[str, Any]:
    member_total = sum(len(p.member_rows) for p in plans)
    catalog_total = sum(len(p.catalog_rows) for p in plans)
    return {
        "snapshot_count": len(plans),
        "meta_rows": len(plans),
        "member_rows": member_total,
        "catalog_rows": catalog_total,
        "publish_rows": len(plans),
        "snapshot_ids": [p.snapshot_id for p in plans],
    }
