"""Batched ClickHouse scope queries (avoids oversized HTTP query payloads)."""

from __future__ import annotations

from typing import Sequence

DEFAULT_SNAPSHOT_ID_CHUNK = 100


def iter_chunks(items: Sequence[str], chunk_size: int = DEFAULT_SNAPSHOT_ID_CHUNK):
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    for i in range(0, len(items), chunk_size):
        yield list(items[i : i + chunk_size])


def count_rows_for_snapshot_ids(
    client,
    db: str,
    table: str,
    snapshot_ids: Sequence[str],
    *,
    chunk_size: int = DEFAULT_SNAPSHOT_ID_CHUNK,
    distinct_snapshot: bool = False,
    extra_where: str = "",
    parameters_extra: dict | None = None,
) -> int:
    if not snapshot_ids:
        return 0
    parameters_extra = parameters_extra or {}
    total = 0
    expr = "countDistinct(snapshot_id)" if distinct_snapshot else "count()"
    for chunk in iter_chunks(list(snapshot_ids), chunk_size):
        params = {"ids": chunk, **parameters_extra}
        q = f"SELECT {expr} FROM {db}.{table} WHERE snapshot_id IN {{ids:Array(String)}}"
        if extra_where:
            q += f" AND ({extra_where})"
        total += int(client.query(q, parameters=params).result_rows[0][0])
    return total


def scope_layer_counts_batched(
    client,
    db: str,
    snapshot_ids: Sequence[str],
    *,
    pipeline_line_id: str | None = None,
    chunk_size: int = DEFAULT_SNAPSHOT_ID_CHUNK,
) -> dict[str, int]:
    """Aggregate member/catalog/meta/sidecar/publish counts for a scope."""
    meta = count_rows_for_snapshot_ids(
        client,
        db,
        "research_lmv2_active_snapshot_meta_v1",
        snapshot_ids,
        chunk_size=chunk_size,
        distinct_snapshot=True,
    )
    members = count_rows_for_snapshot_ids(
        client,
        db,
        "research_lmv2_active_snapshot_member_v1",
        snapshot_ids,
        chunk_size=chunk_size,
    )
    catalog = count_rows_for_snapshot_ids(
        client,
        db,
        "research_lmv2_snapshot_active_pool_v1",
        snapshot_ids,
        chunk_size=chunk_size,
    )
    sidecar_params = {"line": pipeline_line_id} if pipeline_line_id else {}
    sidecar_where = "pipeline_line_id = {line:String}" if pipeline_line_id else "1"
    sidecar = count_rows_for_snapshot_ids(
        client,
        db,
        "research_lmv2_snapshot_import_provenance_v1",
        snapshot_ids,
        chunk_size=chunk_size,
        distinct_snapshot=True,
        extra_where=sidecar_where if pipeline_line_id else "",
        parameters_extra=sidecar_params if pipeline_line_id else None,
    )
    pub_where = "pipeline_line_id = {line:String}" if pipeline_line_id else "1"
    publish = count_rows_for_snapshot_ids(
        client,
        db,
        "research_lmv2_active_snapshot_publish_v1",
        snapshot_ids,
        chunk_size=chunk_size,
        distinct_snapshot=True,
        extra_where=pub_where if pipeline_line_id else "",
        parameters_extra={"line": pipeline_line_id} if pipeline_line_id else None,
    )
    return {
        "meta_distinct": meta,
        "members": members,
        "catalog": catalog,
        "sidecar_distinct": sidecar,
        "publish_distinct": publish,
    }
