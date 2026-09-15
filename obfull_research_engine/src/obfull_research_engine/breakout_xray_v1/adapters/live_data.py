"""Live Silver / readiness / trades adapters (SQL prepared; not executed under hold)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from ..execution_hold import DEFAULT_SILVER_LOCK, assert_full_run_allowed
from ..ports import (
    LevelChangeEvent,
    MidState,
    ReadinessResult,
)
from ..trades import DedupStats, XRayTrade, dedup_trades_by_id
from ..time_windows import ns_to_dt
from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
    ANALYSIS_QUERY_SETTINGS,
    analysis_query_settings,
)


class QueryClient(Protocol):
    def query(self, sql: str, parameters: dict[str, Any] | None = None, settings: dict | None = None) -> Any: ...


def metrics_mid_sql() -> str:
    return (
        "SELECT "
        "toInt64(toUnixTimestamp64Nano(bucket_start)) AS bucket_start_ns, "
        "mid, spread, best_bid, best_ask, book_hash, "
        "bid_level_count, ask_level_count "
        "FROM {db}.ob_metrics_100ms "
        "WHERE symbol = {symbol:String} "
        "AND chunk_key IN ({chunk_keys:Array(String)}) "
        "AND toInt64(toUnixTimestamp64Nano(bucket_start)) >= {start_ns:Int64} "
        "AND toInt64(toUnixTimestamp64Nano(bucket_start)) < {end_ns:Int64} "
        "ORDER BY bucket_start_ns "
        "SETTINGS max_threads = 1"
    )


def level_changes_sql() -> str:
    return (
        "SELECT "
        "toInt64(toUnixTimestamp64Nano(event_time)) AS event_time_ns, "
        "side, price, change_type, new_size, old_size "
        "FROM {db}.ob_level_changes "
        "WHERE symbol = {symbol:String} "
        "AND chunk_key IN ({chunk_keys:Array(String)}) "
        "AND toInt64(toUnixTimestamp64Nano(event_time)) >= {start_ns:Int64} "
        "AND toInt64(toUnixTimestamp64Nano(event_time)) < {end_ns:Int64} "
        "ORDER BY event_time_ns, side, price "
        "SETTINGS max_threads = 1"
    )


def public_trades_sql() -> str:
    return (
        "SELECT trade_ts, trade_id, side, price, size, notional "
        "FROM {db}.public_trades "
        "WHERE symbol = {symbol:String} "
        "AND toInt64(toUnixTimestamp64Nano(trade_ts)) >= {start_ns:Int64} "
        "AND toInt64(toUnixTimestamp64Nano(trade_ts)) < {end_ns:Int64} "
        "ORDER BY trade_ts, trade_id "
        "SETTINGS max_threads = 1"
    )


def normalize_mid_rows(rows: list[dict[str, Any]]) -> list[MidState]:
    out: list[MidState] = []
    seen: set[int] = set()
    for r in rows:
        ns = int(r["bucket_start_ns"])
        if ns in seen:
            continue
        seen.add(ns)
        out.append(
            MidState(
                bucket_start_ns=ns,
                mid=float(r["mid"]),
                spread=None if r.get("spread") is None else float(r["spread"]),
                best_bid=None if r.get("best_bid") is None else float(r["best_bid"]),
                best_ask=None if r.get("best_ask") is None else float(r["best_ask"]),
                book_hash=str(r.get("book_hash") or ""),
                bid_level_count=int(r.get("bid_level_count") or 0),
                ask_level_count=int(r.get("ask_level_count") or 0),
            )
        )
    out.sort(key=lambda m: m.bucket_start_ns)
    return out


def mid_series_quality(series: list[MidState], *, start_ns: int, end_ns: int) -> dict[str, Any]:
    expected = max(0, (end_ns - start_ns) // 100_000_000)
    gaps = 0
    for a, b in zip(series, series[1:]):
        if b.bucket_start_ns - a.bucket_start_ns != 100_000_000:
            gaps += 1
    mono = all(
        series[i].bucket_start_ns < series[i + 1].bucket_start_ns
        for i in range(len(series) - 1)
    )
    dup_buckets = len(series) != len({m.bucket_start_ns for m in series})
    half_open_ok = all(start_ns <= m.bucket_start_ns < end_ns for m in series)
    return {
        "expected_100ms_count": expected,
        "actual_count": len(series),
        "monotonic": mono,
        "duplicate_buckets": dup_buckets,
        "half_open_ok": half_open_ok,
        "gap_count": gaps,
    }


def normalize_level_change_rows(rows: list[dict[str, Any]]) -> list[LevelChangeEvent]:
    out = [
        LevelChangeEvent(
            event_time_ns=int(r["event_time_ns"]),
            side=str(r["side"]),
            price=float(r["price"]),
            change_type=str(r["change_type"]),
            new_size=float(r["new_size"]),
            old_size=None if r.get("old_size") is None else float(r["old_size"]),
        )
        for r in rows
    ]
    out.sort(key=lambda e: (e.event_time_ns, e.side, e.price))
    return out


def normalize_trade_rows(rows: list[dict[str, Any]]) -> list[XRayTrade]:
    trades = [
        XRayTrade(
            trade_ts=r["trade_ts"]
            if isinstance(r["trade_ts"], datetime)
            else ns_to_dt(int(r["trade_ts_ns"]))
            if "trade_ts_ns" in r
            else datetime.fromisoformat(str(r["trade_ts"]).replace("Z", "+00:00")),
            trade_id=str(r["trade_id"]),
            side=str(r["side"]),
            price=float(r["price"]),
            size=float(r["size"]),
            notional=float(r["notional"]),
        )
        for r in rows
    ]
    trades.sort(key=lambda t: (t.trade_ts, t.trade_id))
    return trades


class LiveSilverMetricsRepository:
    def __init__(
        self,
        client: QueryClient,
        *,
        database: str,
        lock_path=DEFAULT_SILVER_LOCK,
    ):
        self.client = client
        self.database = database
        self.lock_path = lock_path

    def load_mid_series(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[MidState]:
        assert_full_run_allowed(self.lock_path)
        if not chunk_keys:
            return []
        sql = metrics_mid_sql().replace("{db}", self.database)
        params = {
            "symbol": symbol,
            "chunk_keys": list(chunk_keys),
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
        }
        settings = analysis_query_settings()
        assert settings["max_threads"] == 1
        assert settings["readonly"] == 1
        raw = self.client.query(sql, parameters=params, settings=settings)
        rows = _rows_from_result(raw)
        return normalize_mid_rows(rows)


class LiveSilverLevelChangesRepository:
    def __init__(self, client: QueryClient, *, database: str, lock_path=DEFAULT_SILVER_LOCK):
        self.client = client
        self.database = database
        self.lock_path = lock_path

    def load_level_changes(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[LevelChangeEvent]:
        assert_full_run_allowed(self.lock_path)
        if not chunk_keys:
            return []
        sql = level_changes_sql().replace("{db}", self.database)
        params = {
            "symbol": symbol,
            "chunk_keys": list(chunk_keys),
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
        }
        raw = self.client.query(
            sql, parameters=params, settings=analysis_query_settings()
        )
        return normalize_level_change_rows(_rows_from_result(raw))


class LivePublicTradesRepository:
    def __init__(self, client: QueryClient, *, database: str, lock_path=DEFAULT_SILVER_LOCK):
        self.client = client
        self.database = database
        self.lock_path = lock_path

    def load_trades(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> tuple[list[XRayTrade], DedupStats]:
        assert_full_run_allowed(self.lock_path)
        sql = public_trades_sql().replace("{db}", self.database)
        params = {
            "symbol": symbol,
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
        }
        raw = self.client.query(
            sql, parameters=params, settings=analysis_query_settings()
        )
        trades = normalize_trade_rows(_rows_from_result(raw))
        return dedup_trades_by_id(trades)


class LiveReadinessRepository:
    """Fail-closed wrapper around analysis_readiness_v1_3.assess_analysis_window.

    Live CH ledger loads are gated by execution hold. Unit tests inject
    ``ready_chunks`` / ``gap_times`` and never touch a real client.
    """

    def __init__(
        self,
        client: QueryClient | None = None,
        *,
        database: str = "",
        lock_path=DEFAULT_SILVER_LOCK,
        ready_chunks: list[Any] | None = None,
        gap_times: list[int] | None = None,
    ):
        self.client = client
        self.database = database
        self.lock_path = lock_path
        self.ready_chunks = ready_chunks
        self.gap_times = gap_times

    def assess_window(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> ReadinessResult:
        from obfull_research_engine.clickhouse_research_store_v1 import (
            analysis_readiness_v1_3 as ar,
        )

        try:
            if self.ready_chunks is not None:
                assessment = ar.assess_analysis_window(
                    start_ns=int(start_ns),
                    end_ns=int(end_ns),
                    ready_chunks=self.ready_chunks,
                    gap_times=list(self.gap_times or []),
                )
            else:
                assert_full_run_allowed(self.lock_path)
                if self.client is None:
                    return ReadinessResult(
                        status="NOT_READY",
                        reason="STOP_XRAY_READINESS_CLIENT_MISSING",
                    )
                # Live path reserved post-hold — still fail-closed if reached without inject
                return ReadinessResult(
                    status="NOT_READY",
                    reason="STOP_XRAY_LIVE_READINESS_NOT_ENABLED_UNDER_EXECUTION_HOLD",
                    detail={"symbol": symbol, "database": self.database},
                )
        except Exception as exc:  # fail-closed
            return ReadinessResult(
                status="NOT_READY",
                reason=f"READINESS_ERROR:{type(exc).__name__}",
                detail={"error": str(exc)},
            )
        if assessment.status != "READY":
            return ReadinessResult(
                status="NOT_READY",
                reason=assessment.reason or "NOT_READY",
                detail=_safe_dict(assessment),
            )
        return ReadinessResult(
            status="READY",
            reason=assessment.reason or "READY",
            epoch_id=str(assessment.epoch_id or ""),
            chunk_keys=tuple(assessment.chunk_keys or ()),
            level_change_count=int(assessment.level_change_count or 0),
            state_count=int(assessment.state_count or 0),
            detail=_safe_dict(assessment),
        )


class StubAvrLiveRepository:
    def load_avr(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        return {"status": "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"}


class StubOpenInterestLiveRepository:
    def load_oi(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        return {"status": "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"}


class LiveMarketProfileRepository:
    def __init__(self, client: QueryClient | None = None, *, lock_path=DEFAULT_SILVER_LOCK):
        self.client = client
        self.lock_path = lock_path

    def load_previous_closed_30m_tpo(
        self, *, symbol: str, decision_time: datetime
    ) -> dict[str, Any]:
        assert_full_run_allowed(self.lock_path)
        from ..adapters import market_profile as mp

        return mp.load_strategy_tpo_edge_profile(
            symbol=symbol, decision_time=decision_time, client=self.client
        )


def _rows_from_result(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if hasattr(raw, "named_results"):
        return list(raw.named_results())
    if hasattr(raw, "result_rows") and hasattr(raw, "column_names"):
        cols = list(raw.column_names)
        return [dict(zip(cols, row)) for row in raw.result_rows]
    raise TypeError(f"unsupported query result type: {type(raw)}")


def _safe_dict(obj: Any) -> dict[str, Any]:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    return {"repr": repr(obj)}


# Re-export settings for tests
QUERY_SETTINGS = dict(ANALYSIS_QUERY_SETTINGS)
