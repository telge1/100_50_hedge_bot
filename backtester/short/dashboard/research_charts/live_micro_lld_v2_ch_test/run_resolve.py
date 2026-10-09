"""Resolve CH test runs by PD identity — no silent preset fallback."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import clickhouse_connect

from ..clickhouse_config import load_clickhouse_config
from .errors import AmbiguousRunError, ConfigNotAvailableError
from .identity import LEGACY_RUN_OVERRIDES, config_hash, effective_config_dict
from .identity import build_live_micro_lld_v2_config
from .schema import TABLE_RUN


def _row_dict(cols: list[str], row: tuple) -> dict[str, Any]:
    return dict(zip(cols, row))


def _enrich_run_meta(meta: dict[str, Any]) -> dict[str, Any]:
    rid = str(meta.get("run_id"))
    legacy = LEGACY_RUN_OVERRIDES.get(rid, {})
    if legacy:
        for k, v in legacy.items():
            if meta.get(k) in (None, "", 0) or (
                k == "from_close"
                and hasattr(meta.get("from_close"), "year")
                and meta["from_close"].year < 2000
            ):
                meta[k] = v
    if meta.get("engine_version") in (None, ""):
        meta["engine_version"] = "live_micro_lld_v2@trp"
    return meta


class RunSelector:
    def __init__(self) -> None:
        self._cfg = load_clickhouse_config()

    def _client(self):
        return clickhouse_connect.get_client(**self._cfg.connect_kwargs())

    def list_runs(
        self,
        symbol: str,
        timeframe: str,
        pool_detail: int,
        *,
        config_hash_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        db = self._cfg.database
        q = f"""
        SELECT run_id, symbol, timeframe, pool_detail, engine_label, engine_version,
               candle_load_start, candle_first_open, candle_last_open, decision_close,
               from_close, to_close, candle_count, pool_count, data_complete,
               config_hash, engine_input_hash, candle_fingerprint, config_json,
               created_at
        FROM {db}.{TABLE_RUN}
        WHERE symbol = {{sym:String}}
          AND timeframe = {{tf:String}}
          AND pool_detail = {{pd:UInt8}}
        """
        params: dict[str, Any] = {
            "sym": str(symbol).upper(),
            "tf": str(timeframe),
            "pd": int(pool_detail),
        }
        if config_hash_filter:
            q += " AND config_hash = {ch:String}"
            params["ch"] = config_hash_filter
        q += " ORDER BY created_at DESC"
        client = self._client()
        try:
            res = client.query(q, parameters=params)
            return [_enrich_run_meta(_row_dict(res.column_names, r)) for r in res.result_rows]
        finally:
            client.close()

    def resolve_run(
        self,
        symbol: str,
        timeframe: str,
        pool_detail: int,
        *,
        run_id: str | None = None,
        require_config_hash: str | None = None,
    ) -> dict[str, Any]:
        sym = str(symbol).upper()
        tf = str(timeframe)
        pd = int(pool_detail)
        expected_hash = require_config_hash or config_hash(build_live_micro_lld_v2_config(pd))

        if run_id:
            meta = self.load_run_by_id(run_id)
            if not meta:
                raise ConfigNotAvailableError(
                    f"unknown run_id {run_id}",
                    {"run_id": run_id},
                )
            if str(meta.get("symbol", "")).upper() != sym or meta.get("timeframe") != tf:
                raise ConfigNotAvailableError("run_id symbol/timeframe mismatch", meta)
            if int(meta.get("pool_detail", -1)) != pd:
                raise ConfigNotAvailableError(
                    "run_id pool_detail mismatch",
                    {"run_id": run_id, "expected_pool_detail": pd, "actual": meta.get("pool_detail")},
                )
            if str(meta.get("config_hash")) != expected_hash:
                raise ConfigNotAvailableError(
                    "run_id config_hash mismatch",
                    {"run_id": run_id, "expected_config_hash": expected_hash},
                )
            return _enrich_run_meta(meta)

        runs = self.list_runs(sym, tf, pd, config_hash_filter=expected_hash)
        complete = [r for r in runs if int(r.get("data_complete") or 0) == 1]
        if not complete:
            raise ConfigNotAvailableError(
                f"no CH test run for {sym} {tf} PD{pd}",
                {"symbol": sym, "timeframe": tf, "pool_detail": pd, "config_hash": expected_hash},
            )
        if len(complete) > 1:
            # Disambiguate by identical engine_input_hash + decision_close
            keys = {
                (
                    str(r.get("engine_input_hash") or ""),
                    str(r.get("decision_close")),
                )
                for r in complete
            }
            if len(keys) > 1:
                raise AmbiguousRunError(
                    "multiple CH test runs match PD identity",
                    {"candidates": [str(r.get("run_id")) for r in complete[:10]]},
                )
        return _enrich_run_meta(complete[0])

    def load_run_by_id(self, run_id: str) -> dict[str, Any] | None:
        db = self._cfg.database
        q = f"""
        SELECT run_id, symbol, timeframe, pool_detail, engine_label, engine_version,
               candle_load_start, candle_first_open, candle_last_open, decision_close,
               from_close, to_close, candle_count, pool_count, data_complete,
               config_hash, engine_input_hash, candle_fingerprint, config_json,
               created_at
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
            return _enrich_run_meta(_row_dict(res.column_names, res.result_rows[0]))
        finally:
            client.close()
