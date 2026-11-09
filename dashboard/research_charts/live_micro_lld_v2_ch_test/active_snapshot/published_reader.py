"""Read-only access to fully published ACTIVE states (not computed-only snapshots)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from research_charts.live_micro_lld_v2_ch_test.historical_json_import.catalog_integrity import (
    assert_member_catalog_id_sets,
    load_catalog_pool_ids_from_ch,
)
from .fingerprint import active_state_fingerprint
from .pool_catalog_schema import TABLE_ACTIVE_POOL
from .publish_schema import TABLE_PUBLISH, ensure_publish_tables
from .reader import ActiveSnapshotReader, IncompleteSnapshotError
from .schema import TABLE_SNAPSHOT_META


class UnpublishedSnapshotError(Exception):
    pass


class PublishedActiveReader:
    """
    Visibility rule: a decision_close is readable only if
    - publish ledger row with publish_complete=1 (FINAL)
    - meta row write_complete=1 (FINAL) for snapshot_id
    - member count matches meta
    - catalog row count and pool_id set match members (not JSON field parity)
    - member-derived fingerprint matches publish + meta fingerprints
    """

    def __init__(self) -> None:
        ensure_publish_tables()
        self._ch_cfg = load_clickhouse_config()
        self._db = self._ch_cfg.database
        self._snap_reader = ActiveSnapshotReader()

    def _client(self):
        return clickhouse_connect.get_client(**self._ch_cfg.connect_kwargs())

    def list_published_closes(
        self,
        pipeline_line_id: str,
        *,
        config_hash: str,
    ) -> list[datetime]:
        client = self._client()
        try:
            q = f"""
            SELECT decision_close
            FROM {self._db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND config_hash = {{ch:String}}
              AND publish_complete = 1
            ORDER BY decision_close ASC
            """
            rows = client.query(
                q,
                parameters={"lid": pipeline_line_id, "ch": config_hash},
            ).result_rows
            out = []
            for r in rows:
                dc = r[0]
                if dc.tzinfo is None:
                    dc = dc.replace(tzinfo=timezone.utc)
                out.append(dc)
            return out
        finally:
            client.close()

    def load_published(
        self,
        pipeline_line_id: str,
        decision_close: datetime,
        *,
        config_hash: str,
    ) -> dict[str, Any]:
        client = self._client()
        try:
            q = f"""
            SELECT publish_id, snapshot_id, active_state_fingerprint, prefix_candle_fingerprint,
                   engine_input_hash, engine_version
            FROM {self._db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND config_hash = {{ch:String}}
              AND decision_close = {{dc:DateTime64(3, 'UTC')}}
              AND publish_complete = 1
            LIMIT 1
            """
            rows = client.query(
                q,
                parameters={
                    "lid": pipeline_line_id,
                    "ch": config_hash,
                    "dc": decision_close,
                },
            ).result_rows
            if not rows:
                raise UnpublishedSnapshotError(
                    f"no published state for {pipeline_line_id} @ {decision_close}"
                )
            pub_id, sid, pub_fp, prefix_fp, eih, ev = rows[0]
            meta = self._snap_reader.load_meta(sid)
            pool_ids, sides = self._snap_reader.load_active_members(sid)
            derived = active_state_fingerprint(pool_ids, sides)
            if derived != pub_fp or derived != meta["active_state_fingerprint"]:
                raise IncompleteSnapshotError(
                    "published fingerprint does not match stored members/meta"
                )
            if meta["prefix_candle_fingerprint"] != prefix_fp:
                raise IncompleteSnapshotError("prefix_candle_fingerprint mismatch vs publish")
            if pool_ids:
                cat_ids = load_catalog_pool_ids_from_ch(
                    client, self._db, TABLE_ACTIVE_POOL, sid
                )
                if len(cat_ids) != len(pool_ids):
                    raise IncompleteSnapshotError(
                        f"catalog incomplete for {sid}: {len(cat_ids)} rows vs {len(pool_ids)} members"
                    )
                try:
                    assert_member_catalog_id_sets(pool_ids, cat_ids)
                except Exception as exc:
                    raise IncompleteSnapshotError(str(exc)) from exc
            return {
                "publish_id": pub_id,
                "snapshot_id": sid,
                "decision_close": decision_close,
                "active_state_fingerprint": pub_fp,
                "prefix_candle_fingerprint": prefix_fp,
                "engine_input_hash": eih,
                "engine_version": ev,
                "pool_ids": pool_ids,
                "sides": sides,
                "meta": meta,
            }
        finally:
            client.close()

    def find_gaps(
        self,
        pipeline_line_id: str,
        expected_closes: list[datetime],
        *,
        config_hash: str,
    ) -> tuple[list[datetime], list[datetime]]:
        published = set(self.list_published_closes(pipeline_line_id, config_hash=config_hash))
        exp = [c.astimezone(timezone.utc) for c in expected_closes]
        missing = [c for c in exp if c not in published]
        extra = [c for c in published if c not in set(exp)]
        return missing, extra

    def count_published(self, pipeline_line_id: str, *, config_hash: str) -> int:
        return len(self.list_published_closes(pipeline_line_id, config_hash=config_hash))

    def list_published_summary(
        self,
        pipeline_line_id: str,
        *,
        config_hash: str,
    ) -> list[dict[str, Any]]:
        client = self._client()
        try:
            q = f"""
            SELECT decision_close, snapshot_id, active_state_fingerprint, prefix_candle_fingerprint
            FROM {self._db}.{TABLE_PUBLISH} FINAL
            WHERE pipeline_line_id = {{lid:String}}
              AND config_hash = {{ch:String}}
              AND publish_complete = 1
            ORDER BY decision_close ASC
            """
            rows = client.query(
                q,
                parameters={"lid": pipeline_line_id, "ch": config_hash},
            ).result_rows
            return [
                {
                    "decision_close": r[0],
                    "snapshot_id": r[1],
                    "active_state_fingerprint": r[2],
                    "prefix_candle_fingerprint": r[3],
                }
                for r in rows
            ]
        finally:
            client.close()
