"""Read only complete ACTIVE snapshots from isolated CH tables."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import clickhouse_connect

from ...clickhouse_config import load_clickhouse_config
from .schema import TABLE_SNAPSHOT_MEMBER, TABLE_SNAPSHOT_META, ensure_active_snapshot_tables


class IncompleteSnapshotError(Exception):
    pass


class SnapshotNotFoundError(Exception):
    pass


class ActiveSnapshotReader:
    def __init__(self) -> None:
        ensure_active_snapshot_tables()
        self._ch_cfg = load_clickhouse_config()
        self._db = self._ch_cfg.database

    def _client(self):
        return clickhouse_connect.get_client(**self._ch_cfg.connect_kwargs())

    def load_meta(self, snapshot_id: str) -> dict[str, Any]:
        client = self._client()
        try:
            q = f"""
            SELECT
                snapshot_id, test_run_id, symbol, timeframe, pool_detail, config_hash,
                engine_version, engine_input_hash, candle_load_start, history_from_close,
                decision_close, prefix_candle_count, prefix_candle_fingerprint,
                known_pool_count, active_pool_count, active_upper_count, active_lower_count,
                active_state_fingerprint, write_complete
            FROM {self._db}.{TABLE_SNAPSHOT_META} FINAL
            WHERE snapshot_id = {{sid:String}}
            LIMIT 1
            """
            rows = client.query(q, parameters={"sid": snapshot_id}).result_rows
            if not rows:
                raise SnapshotNotFoundError(snapshot_id)
            r = rows[0]
            if int(r[18]) != 1:
                raise IncompleteSnapshotError(f"snapshot {snapshot_id} write_complete != 1")
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
        finally:
            client.close()

    def load_active_members(self, snapshot_id: str) -> tuple[list[str], list[str]]:
        meta = self.load_meta(snapshot_id)
        client = self._client()
        try:
            q = f"""
            SELECT pool_id, side
            FROM {self._db}.{TABLE_SNAPSHOT_MEMBER}
            WHERE snapshot_id = {{sid:String}}
            ORDER BY sort_index ASC, pool_id ASC
            """
            rows = client.query(q, parameters={"sid": snapshot_id}).result_rows
            pool_ids = [r[0] for r in rows]
            sides = [r[1] for r in rows]
            if len(pool_ids) != meta["active_pool_count"]:
                raise IncompleteSnapshotError(
                    f"member count {len(pool_ids)} != meta active_pool_count {meta['active_pool_count']}"
                )
            return pool_ids, sides
        finally:
            client.close()

    def find_complete_snapshot(
        self,
        *,
        symbol: str,
        timeframe: str,
        pool_detail: int,
        config_hash: str,
        decision_close: datetime,
    ) -> str | None:
        client = self._client()
        try:
            q = f"""
            SELECT snapshot_id
            FROM {self._db}.{TABLE_SNAPSHOT_META} FINAL
            WHERE symbol = {{sym:String}}
              AND timeframe = {{tf:String}}
              AND pool_detail = {{pd:UInt8}}
              AND config_hash = {{ch:String}}
              AND decision_close = {{dc:DateTime64(3, 'UTC')}}
              AND write_complete = 1
            ORDER BY created_at DESC
            LIMIT 1
            """
            rows = client.query(
                q,
                parameters={
                    "sym": symbol.upper(),
                    "tf": timeframe,
                    "pd": int(pool_detail),
                    "ch": config_hash,
                    "dc": decision_close,
                },
            ).result_rows
            return rows[0][0] if rows else None
        finally:
            client.close()
