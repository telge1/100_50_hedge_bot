"""Read-only access to isolated Live Micro LLD V2 CH test snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import clickhouse_connect

from ..chart_live_micro_lld_v2 import pools_to_zones
from ..clickhouse_config import load_clickhouse_config
from ..trp_import import ensure_trp_on_path
from .schema import TABLE_POOL, TABLE_RUN


def _row_to_pool_dict(row: dict[str, Any]) -> dict[str, Any]:
    def iso(v):
        if v is None:
            return None
        if hasattr(v, "isoformat"):
            return v.isoformat()
        return str(v)

    return {
        "pool_id": row["pool_id"],
        "pool_type": row["pool_type"],
        "symbol": row["symbol"],
        "timeframe": row["timeframe"],
        "side": row["side"],
        "source_timestamp": iso(row["source_timestamp"]),
        "created_timestamp": iso(row["created_timestamp"]),
        "known_at": iso(row["known_at"]),
        "top_price": float(row["top_price"]),
        "bottom_price": float(row["bottom_price"]),
        "strength": float(row["strength"]),
        "strength_class": row["strength_class"],
        "active": bool(int(row.get("active", 0))),
        "break_at": iso(row.get("break_at")),
        "pivot_timestamp": iso(row.get("pivot_timestamp")),
        "early_timing": row.get("early_timing"),
        "reclaim_level": row.get("reclaim_level"),
        "candidate_created_at": iso(row.get("candidate_created_at")),
        "confirmed_at": iso(row.get("confirmed_at")),
        "invalidated_at": iso(row.get("invalidated_at")),
        "created_index": int(row.get("created_index", 0)),
    }


class LiveMicroLldV2ChTestReader:
    """A: full pool set for run; B: as-of visible pools; C: chart segments via pools_to_zones."""

    def __init__(self) -> None:
        self._cfg = load_clickhouse_config()

    def _client(self):
        return clickhouse_connect.get_client(**self._cfg.connect_kwargs())

    def load_run_meta(self, run_id: str) -> dict[str, Any] | None:
        db = self._cfg.database
        q = f"""
        SELECT run_id, symbol, timeframe, pool_detail, decision_close, candle_load_start,
               candle_first_open, candle_last_open, candle_count,
               pool_count, data_complete, config_hash
        FROM {db}.{TABLE_RUN}
        WHERE run_id = {{rid:UUID}}
        ORDER BY created_at DESC
        LIMIT 1
        """
        client = self._client()
        try:
            res = client.query(q, parameters={"rid": run_id})
            if not res.result_rows:
                return None
            cols = res.column_names
            return dict(zip(cols, res.result_rows[0]))
        finally:
            client.close()

    def load_all_pools(self, run_id: str) -> list[dict[str, Any]]:
        """All pools persisted for a batch run (includes broken/historical)."""
        db = self._cfg.database
        q = f"""
        SELECT *
        FROM {db}.{TABLE_POOL} FINAL
        WHERE run_id = {{rid:UUID}}
        ORDER BY pool_id
        """
        client = self._client()
        try:
            res = client.query(q, parameters={"rid": run_id})
            cols = res.column_names
            return [dict(zip(cols, row)) for row in res.result_rows]
        finally:
            client.close()

    def pools_visible_at(
        self,
        run_id: str,
        as_of_close: datetime,
    ) -> list[dict[str, Any]]:
        """Pools known at or before as_of_close with break_at adjusted per engine as_of rules."""
        ensure_trp_on_path()
        from indicators.live_micro_lld_v2.as_of import pool_as_of

        as_of = as_of_close.astimezone(timezone.utc)
        rows = self.load_all_pools(run_id)
        out: list[dict[str, Any]] = []
        for row in rows:
            d = _row_to_pool_dict(row)
            snap = pool_as_of(d, as_of)
            if snap is not None:
                out.append(snap)
        return out

    def pools_active_at(
        self,
        run_id: str,
        as_of_close: datetime,
    ) -> list[dict[str, Any]]:
        """Subset of pools_visible_at where active is True under engine as_of semantics."""
        return [p for p in self.pools_visible_at(run_id, as_of_close) if p.get("active")]

    def display_segments(
        self,
        run_id: str,
        from_close: datetime,
        to_close: datetime,
        *,
        pool_types: set[str] | None = None,
        strength_classes: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Chart segments for [from_close, to_close] using the same pools_to_zones helper."""
        from_unix = int(from_close.astimezone(timezone.utc).timestamp())
        to_unix = int(to_close.astimezone(timezone.utc).timestamp())
        visible = self.pools_visible_at(run_id, to_close)
        return pools_to_zones(
            visible,
            from_unix=from_unix,
            to_unix=to_unix,
            pool_types=pool_types,
            strength_classes=strength_classes,
        )
