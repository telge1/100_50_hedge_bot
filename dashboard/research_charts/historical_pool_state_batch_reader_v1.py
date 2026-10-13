"""Phase 5S — batched ClickHouse reads for episode walk (optional, parity with PublishedActiveReader)."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

import clickhouse_connect

from research_charts.live_micro_lld_v2_ch_test.active_snapshot.fingerprint import (
    active_state_fingerprint,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.pool_catalog_schema import (
    TABLE_ACTIVE_POOL,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.publish_schema import (
    TABLE_PUBLISH,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.published_reader import (
    UnpublishedSnapshotError,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.reader import (
    IncompleteSnapshotError,
    SnapshotNotFoundError,
)
from research_charts.live_micro_lld_v2_ch_test.active_snapshot.schema import (
    TABLE_SNAPSHOT_MEMBER,
    TABLE_SNAPSHOT_META,
)
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_integrity import (
    assert_member_catalog_id_sets,
)

from .historical_pool_state_v1 import (
    PIPELINE_LINE_ID,
    HistoricalPoolStateError,
    _iso_z,
    _utc,
)

PUBLISH_CLOSE_CHUNK = 128
SNAPSHOT_ID_CHUNK = 64
CATALOG_TABLE = TABLE_ACTIVE_POOL


def historical_lmv2_batch_reader_enabled() -> bool:
    return os.environ.get("HISTORICAL_LMV2_BATCH_READER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _dc_unix(dc: datetime) -> int:
    return int(_utc(dc).timestamp())


def _meta_from_row(r: tuple) -> dict[str, Any]:
    if int(r[18]) != 1:
        raise IncompleteSnapshotError(f"snapshot {r[0]} write_complete != 1")
    return {
        "snapshot_id": r[0],
        "test_run_id": str(r[1]),
        "symbol": r[2],
        "timeframe": r[3],
        "pool_detail": int(r[4]),
        "config_hash": r[5],
        "engine_version": r[6],
        "engine_input_hash": r[7],
        "candle_load_start": r[8],
        "history_from_close": r[9],
        "decision_close": r[10],
        "prefix_candle_count": int(r[11]),
        "prefix_candle_fingerprint": r[12],
        "known_pool_count": int(r[13]),
        "active_pool_count": int(r[14]),
        "active_upper_count": int(r[15]),
        "active_lower_count": int(r[16]),
        "active_state_fingerprint": r[17],
        "write_complete": int(r[18]),
    }


def _chunk(seq: Sequence, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


@dataclass
class EpisodeWalkBatchCache:
    """Request-scoped publication + catalog lookups (same payloads as single-reader path)."""

    pipeline_line_id: str
    config_hash: str
    database: str
    _publications: dict[int, dict[str, Any]] = field(default_factory=dict)
    _catalog: dict[tuple[str, int], list[dict[str, Any]]] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)

    def publication(self, cfg_hash: str, decision_close: datetime) -> dict[str, Any]:
        if cfg_hash != self.config_hash:
            raise ValueError("batch cache config_hash mismatch")
        key = _dc_unix(decision_close)
        if key not in self._publications:
            raise UnpublishedSnapshotError(
                f"no batched publication for {self.pipeline_line_id} @ {decision_close}"
            )
        return copy.deepcopy(self._publications[key])

    def catalog(self, database: str, snapshot_id: str, decision_close: datetime) -> list[dict]:
        if database != self.database:
            raise ValueError("batch cache database mismatch")
        key = (snapshot_id, _dc_unix(decision_close))
        if key not in self._catalog:
            raise KeyError(f"no batched catalog for {snapshot_id} @ {decision_close}")
        return copy.deepcopy(self._catalog[key])


def _query_stats(client, sql: str, parameters: dict) -> Any:
    return client.query(sql, parameters=parameters)


def build_episode_walk_batch_cache(
    client,
    database: str,
    pipeline_line_id: str,
    cfg_hash: str,
    closes: Sequence[datetime],
) -> EpisodeWalkBatchCache:
    """
    Batch-load publish/meta/member/catalog with FINAL where the single-reader uses FINAL.
    Validates each close with the same rules as PublishedActiveReader.load_published.
    """
    cache = EpisodeWalkBatchCache(
        pipeline_line_id=pipeline_line_id,
        config_hash=cfg_hash,
        database=database,
    )
    query_count = 0
    closes_utc = [_utc(c) for c in closes]
    publish_rows: dict[int, tuple] = {}

    for chunk in _chunk(closes_utc, PUBLISH_CLOSE_CHUNK):
        q = f"""
        SELECT decision_close, publish_id, snapshot_id, active_state_fingerprint,
               prefix_candle_fingerprint, engine_input_hash, engine_version
        FROM {database}.{TABLE_PUBLISH} FINAL
        WHERE pipeline_line_id = {{lid:String}}
          AND config_hash = {{ch:String}}
          AND publish_complete = 1
          AND decision_close IN {{dcs:Array(DateTime64(3, 'UTC'))}}
        """
        res = _query_stats(
            client,
            q,
            {"lid": pipeline_line_id, "ch": cfg_hash, "dcs": list(chunk)},
        )
        query_count += 1
        for r in res.result_rows:
            dc = r[0]
            if dc.tzinfo is None:
                dc = dc.replace(tzinfo=timezone.utc)
            publish_rows[_dc_unix(dc)] = r

    snapshot_ids = sorted({str(r[2]) for r in publish_rows.values()})
    meta_by_sid: dict[str, dict[str, Any]] = {}
    members_by_sid: dict[str, tuple[list[str], list[str]]] = {}
    catalog_ids_by_sid: dict[str, list[str]] = {}
    catalog_rows_by_sid: dict[str, list[tuple]] = {}

    for sid_chunk in _chunk(snapshot_ids, SNAPSHOT_ID_CHUNK):
        mq = f"""
        SELECT
            snapshot_id, test_run_id, symbol, timeframe, pool_detail, config_hash,
            engine_version, engine_input_hash, candle_load_start, history_from_close,
            decision_close, prefix_candle_count, prefix_candle_fingerprint,
            known_pool_count, active_pool_count, active_upper_count, active_lower_count,
            active_state_fingerprint, write_complete
        FROM {database}.{TABLE_SNAPSHOT_META} FINAL
        WHERE snapshot_id IN {{sids:Array(String)}}
        """
        mres = _query_stats(client, mq, {"sids": list(sid_chunk)})
        query_count += 1
        for r in mres.result_rows:
            meta_by_sid[str(r[0])] = _meta_from_row(r)

        memq = f"""
        SELECT snapshot_id, pool_id, side
        FROM {database}.{TABLE_SNAPSHOT_MEMBER}
        WHERE snapshot_id IN {{sids:Array(String)}}
        ORDER BY snapshot_id ASC, sort_index ASC, pool_id ASC
        """
        memres = _query_stats(client, memq, {"sids": list(sid_chunk)})
        query_count += 1
        grouped: dict[str, list[tuple]] = {}
        for r in memres.result_rows:
            grouped.setdefault(str(r[0]), []).append((str(r[1]), str(r[2])))
        for sid, pairs in grouped.items():
            meta = meta_by_sid.get(sid)
            if not meta:
                raise SnapshotNotFoundError(sid)
            pool_ids = [p[0] for p in pairs]
            sides = [p[1] for p in pairs]
            if len(pool_ids) != meta["active_pool_count"]:
                raise IncompleteSnapshotError(
                    f"member count {len(pool_ids)} != meta active_pool_count "
                    f"{meta['active_pool_count']} for {sid}"
                )
            members_by_sid[sid] = (pool_ids, sides)

        cidq = f"""
        SELECT snapshot_id, pool_id
        FROM {database}.{CATALOG_TABLE}
        WHERE snapshot_id IN {{sids:Array(String)}}
        ORDER BY snapshot_id ASC, pool_id ASC
        """
        cidres = _query_stats(client, cidq, {"sids": list(sid_chunk)})
        query_count += 1
        cid_group: dict[str, list[str]] = {}
        for r in cidres.result_rows:
            cid_group.setdefault(str(r[0]), []).append(str(r[1]))
        catalog_ids_by_sid.update(cid_group)

        catq = f"""
        SELECT snapshot_id, pool_id, side, pool_type, top_price, bottom_price, strength, strength_class,
               known_at, break_at, confirmed_at, invalidated_at
        FROM {database}.{CATALOG_TABLE}
        WHERE snapshot_id IN {{sids:Array(String)}}
        """
        catres = _query_stats(client, catq, {"sids": list(sid_chunk)})
        query_count += 1
        for r in catres.result_rows:
            catalog_rows_by_sid.setdefault(str(r[0]), []).append(r)

    publications: dict[int, dict[str, Any]] = {}
    catalog_cache: dict[tuple[str, int], list[dict]] = {}

    for dc in closes_utc:
        ts = _dc_unix(dc)
        prow = publish_rows.get(ts)
        if not prow:
            raise UnpublishedSnapshotError(
                f"no published state for {pipeline_line_id} @ {dc}"
            )
        _, pub_id, sid, pub_fp, prefix_fp, eih, ev = prow
        sid = str(sid)
        meta = meta_by_sid.get(sid)
        if not meta:
            raise SnapshotNotFoundError(sid)
        pool_ids, sides = members_by_sid[sid]
        derived = active_state_fingerprint(pool_ids, sides)
        if derived != pub_fp or derived != meta["active_state_fingerprint"]:
            raise IncompleteSnapshotError(
                "published fingerprint does not match stored members/meta"
            )
        if meta["prefix_candle_fingerprint"] != prefix_fp:
            raise IncompleteSnapshotError("prefix_candle_fingerprint mismatch vs publish")
        if pool_ids:
            cat_ids = catalog_ids_by_sid.get(sid, [])
            if len(cat_ids) != len(pool_ids):
                raise IncompleteSnapshotError(
                    f"catalog incomplete for {sid}: {len(cat_ids)} rows vs {len(pool_ids)} members"
                )
            assert_member_catalog_id_sets(pool_ids, cat_ids)

        publications[ts] = {
            "publish_id": pub_id,
            "snapshot_id": sid,
            "decision_close": dc,
            "active_state_fingerprint": pub_fp,
            "prefix_candle_fingerprint": prefix_fp,
            "engine_input_hash": eih,
            "engine_version": ev,
            "pool_ids": list(pool_ids),
            "sides": list(sides),
            "meta": copy.deepcopy(meta),
        }

        cat_key = (sid, ts)
        if cat_key not in catalog_cache:
            raw_rows = catalog_rows_by_sid.get(sid, [])
            out = []
            dc_iso = _iso_z(dc)
            for r in raw_rows:
                known = r[8]
                if known is not None and _utc(known) > dc:
                    raise HistoricalPoolStateError(
                        "CAUSALITY_VIOLATION",
                        f"known_at after decision_close for {r[1]}",
                        {"pool_id": str(r[1]), "decision_close": dc_iso},
                    )
                out.append(
                    {
                        "pool_id": str(r[1]),
                        "side": str(r[2]).lower(),
                        "pool_type": str(r[3]),
                        "top": float(r[4]),
                        "bottom": float(r[5]),
                        "strength": float(r[6]),
                        "strength_class": str(r[7]),
                        "known_at": _iso_z(r[8]) if r[8] is not None else None,
                        "break_at": _iso_z(r[9]) if r[9] is not None else None,
                        "confirmed_at": _iso_z(r[10]) if r[10] is not None else None,
                        "invalidated_at": _iso_z(r[11]) if r[11] is not None else None,
                        "decision_close": dc_iso,
                        "active_membership": True,
                    }
                )
            catalog_cache[cat_key] = out

    cache._publications = publications
    cache._catalog = catalog_cache
    cache.stats = {
        "clickhouse_query_count": query_count,
        "episode_closes": len(closes_utc),
        "unique_snapshot_ids": len(snapshot_ids),
        "publish_rows_loaded": len(publish_rows),
    }
    return cache


def sql_semantics_documentation() -> dict[str, Any]:
    return {
        "publish": {
            "table": TABLE_PUBLISH,
            "engine": "ReplacingMergeTree(created_at)",
            "final": True,
            "filters": ["pipeline_line_id", "config_hash", "publish_complete=1", "decision_close"],
            "batch": "decision_close IN Array (chunked)",
        },
        "meta": {
            "table": TABLE_SNAPSHOT_META,
            "final": True,
            "filters": ["snapshot_id IN Array"],
        },
        "member": {
            "table": TABLE_SNAPSHOT_MEMBER,
            "final": False,
            "order": "sort_index, pool_id",
        },
        "catalog": {
            "table": CATALOG_TABLE,
            "final": False,
            "filters": ["snapshot_id IN Array"],
            "causality": "known_at <= decision_close per row (Python, same as _load_catalog)",
        },
        "reference_reader": "PublishedActiveReader.load_published (unchanged)",
        "pipeline_line_isolation": PIPELINE_LINE_ID,
    }
