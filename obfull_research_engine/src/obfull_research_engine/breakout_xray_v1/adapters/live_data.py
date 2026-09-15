"""Live Silver / readiness / trades adapters (SQL prepared; gated execution).

Table names and payload layouts follow silver_full_build_v1_3 + silver_replay.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Protocol, Sequence

from ..execution_hold import (
    DEFAULT_SILVER_LOCK,
    ExecutionSentinel,
    assert_full_run_allowed,
)
from ..ports import LevelChangeEvent, MidState, ReadinessResult
from ..trades import XRayTrade
from ..time_windows import ns_to_dt
from obfull_research_engine.clickhouse_research_store_v1.analysis_readiness_v1_3 import (
    ANALYSIS_QUERY_SETTINGS,
    analysis_query_settings,
    _analysis_query,
)
from obfull_research_engine.clickhouse_research_store_v1.silver_full_build_v1_3 import (
    DEFAULT_OUTPUT_DATABASE,
    LEVEL_CHANGES_TABLE,
    METRICS_TABLE,
    validate_output_database,
)


class QueryClient(Protocol):
    def query(
        self,
        sql: str,
        parameters: dict[str, Any] | None = None,
        settings: dict | None = None,
    ) -> Any: ...


PUBLIC_TRADES_TABLE = "orderbook_analysis.public_trades_canonical"
BUCKET_NS = 100_000_000
STOP_LC_PRICE_BAND_TOO_WIDE = "STOP_LC_PRICE_BAND_TOO_WIDE"
DEFAULT_MAX_LC_PRICE_SPAN_USD = 2_500.0

FORBIDDEN_SQL_TOKENS = (
    " insert ",
    " alter ",
    " drop ",
    " truncate ",
    " optimize ",
    " delete ",
    " create ",
    " attach ",
    " detach ",
    " rename ",
    " grant ",
    " revoke ",
)


def validate_database_name(database: str) -> str:
    validate_output_database(database)
    return database


def _strip_sql_noise(sql: str) -> str:
    """Remove line/block comments and string literals for keyword scans."""
    no_line = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    no_block = re.sub(r"/\*.*?\*/", " ", no_line, flags=re.DOTALL)
    no_str = re.sub(r"'([^'\\]|\\.)*'", " '' ", no_block)
    return f" {' '.join(no_str.split()).lower()} "


def assert_select_only_bounded(sql: str) -> None:
    """Additional guard for fixed templates; not a substitute for safe templates."""
    cleaned = _strip_sql_noise(sql)
    # Allow SELECT or WITH ... SELECT
    body = cleaned.lstrip()
    if body.startswith("with "):
        if " select " not in f" {body} ":
            raise RuntimeError("STOP_XRAY_ANALYSIS_SELECT_ONLY")
    elif not body.startswith("select "):
        raise RuntimeError("STOP_XRAY_ANALYSIS_SELECT_ONLY")
    if ";" in cleaned.strip().rstrip(";"):
        # stacked statements
        raise RuntimeError("STOP_XRAY_ANALYSIS_SELECT_ONLY")
    if any(tok in cleaned for tok in FORBIDDEN_SQL_TOKENS):
        raise RuntimeError("STOP_XRAY_ANALYSIS_SELECT_ONLY")
    if "ob_metrics" in cleaned or "ob_level_changes" in cleaned:
        if "chunk_key" not in cleaned:
            raise RuntimeError("STOP_XRAY_UNBOUNDED_SCAN:chunk_key_required")
        if "bucket_start_ns" not in cleaned and "event_time_ns" not in cleaned:
            raise RuntimeError("STOP_XRAY_UNBOUNDED_SCAN:time_bound_required")
    if "public_trades" in cleaned:
        if "symbol" not in cleaned:
            raise RuntimeError("STOP_XRAY_UNBOUNDED_SCAN:symbol_required")
        if "trade_ts" not in cleaned:
            raise RuntimeError("STOP_XRAY_UNBOUNDED_SCAN:trade_ts_required")
    if "ob_level_changes" in cleaned:
        if "jsonextractfloat" not in cleaned.replace(" ", ""):
            raise RuntimeError("STOP_XRAY_UNBOUNDED_SCAN:price_band_required")
        if "price_min" not in cleaned or "price_max" not in cleaned:
            raise RuntimeError("STOP_XRAY_UNBOUNDED_SCAN:price_band_required")


def metrics_mid_sql(*, database: str) -> str:
    database = validate_database_name(database)
    return f"""
SELECT
  bucket_start_ns,
  chunk_key,
  payload
FROM {database}.{METRICS_TABLE} FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key IN {{chunk_keys:Array(String)}}
  AND bucket_start_ns >= {{start_ns:UInt64}}
  AND bucket_start_ns < {{end_ns:UInt64}}
ORDER BY bucket_start_ns, chunk_key
""".strip()


def metrics_one_bucket_sql(*, database: str) -> str:
    database = validate_database_name(database)
    return f"""
SELECT
  bucket_start_ns,
  chunk_key,
  payload
FROM {database}.{METRICS_TABLE} FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key IN {{chunk_keys:Array(String)}}
  AND bucket_start_ns = {{bucket_start_ns:UInt64}}
ORDER BY chunk_key
LIMIT 1
""".strip()


def level_changes_sql(*, database: str) -> str:
    database = validate_database_name(database)
    # price lives in JSON payload (silver_replay level-change rows)
    return f"""
SELECT
  event_time_ns,
  chunk_key,
  apply_order,
  record_ordinal,
  payload
FROM {database}.{LEVEL_CHANGES_TABLE} FINAL
WHERE symbol = {{symbol:String}}
  AND chunk_key IN {{chunk_keys:Array(String)}}
  AND event_time_ns >= {{start_ns:UInt64}}
  AND event_time_ns < {{end_ns:UInt64}}
  AND JSONExtractFloat(payload, 'price') >= {{price_min:Float64}}
  AND JSONExtractFloat(payload, 'price') <= {{price_max:Float64}}
ORDER BY event_time_ns, apply_order, chunk_key, record_ordinal
""".strip()


def public_trades_sql() -> str:
    return f"""
SELECT
  trade_ts,
  trade_id,
  side,
  price,
  size,
  notional,
  ingest_timestamp,
  source
FROM {PUBLIC_TRADES_TABLE}
WHERE symbol = {{symbol:String}}
  AND trade_ts >= {{start_ts:DateTime64(9, 'UTC')}}
  AND trade_ts < {{end_ts:DateTime64(9, 'UTC')}}
ORDER BY trade_ts, trade_id
""".strip()


def assert_price_band_width(
    price_min: float, price_max: float, *, max_span_usd: float = DEFAULT_MAX_LC_PRICE_SPAN_USD
) -> None:
    if float(price_max) < float(price_min):
        raise RuntimeError("STOP_LC_PRICE_BAND_INVALID")
    if float(price_max) - float(price_min) > float(max_span_usd):
        raise RuntimeError(
            f"{STOP_LC_PRICE_BAND_TOO_WIDE}:span={float(price_max) - float(price_min)}"
            f">max={max_span_usd}"
        )


def _parse_payload(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    return json.loads(str(raw))


def normalize_mid_rows(rows: list[dict[str, Any]]) -> list[MidState]:
    out: list[MidState] = []
    seen: set[int] = set()
    for r in rows:
        ns = int(r["bucket_start_ns"])
        if ns in seen:
            continue
        seen.add(ns)
        payload = _parse_payload(r.get("payload"))
        # Prefer top-level overrides when present (tests / denormalized mocks)
        mid = r.get("mid", payload.get("mid"))
        if mid is None:
            continue
        out.append(
            MidState(
                bucket_start_ns=ns,
                mid=float(mid),
                spread=_opt_float(r.get("spread", payload.get("spread"))),
                best_bid=_opt_float(r.get("best_bid", payload.get("best_bid"))),
                best_ask=_opt_float(r.get("best_ask", payload.get("best_ask"))),
                book_hash=str(r.get("book_hash", payload.get("book_hash") or "")),
                bid_level_count=int(
                    r.get("bid_level_count", payload.get("bid_level_count") or 0)
                ),
                ask_level_count=int(
                    r.get("ask_level_count", payload.get("ask_level_count") or 0)
                ),
            )
        )
    out.sort(key=lambda m: m.bucket_start_ns)
    return out


def mid_series_quality(
    series: list[MidState], *, start_ns: int, end_ns: int
) -> dict[str, Any]:
    expected = max(0, (end_ns - start_ns) // 100_000_000)
    gaps = 0
    for a, b in zip(series, series[1:]):
        if b.bucket_start_ns - a.bucket_start_ns != 100_000_000:
            gaps += 1
    mono = all(
        series[i].bucket_start_ns < series[i + 1].bucket_start_ns
        for i in range(len(series) - 1)
    )
    return {
        "expected_100ms_count": expected,
        "actual_count": len(series),
        "monotonic": mono,
        "duplicate_buckets": len(series) != len({m.bucket_start_ns for m in series}),
        "half_open_ok": all(start_ns <= m.bucket_start_ns < end_ns for m in series),
        "gap_count": gaps,
        # Silver book_hash = state at bucket END (see silver_replay.emit_due_buckets)
        "book_hash_semantics": "state_at_bucket_end_before_next_event",
    }


def normalize_level_change_rows(
    rows: list[dict[str, Any]],
    *,
    ref_price: float | None = None,
    local_band_usd: float | None = None,
) -> list[LevelChangeEvent]:
    out: list[LevelChangeEvent] = []
    for r in rows:
        payload = _parse_payload(r.get("payload"))
        side = str(r.get("side", payload.get("side") or ""))
        price = float(r.get("price", payload.get("price")))
        if ref_price is not None and local_band_usd is not None:
            if abs(price - float(ref_price)) > float(local_band_usd):
                continue
        old = r.get("old_size", payload.get("old_size"))
        new = float(r.get("new_size", payload.get("new_size")))
        out.append(
            LevelChangeEvent(
                event_time_ns=int(r.get("event_time_ns", payload.get("event_time_ns"))),
                side=side,
                price=price,
                change_type=str(r.get("change_type", payload.get("change_type") or "")),
                new_size=new,
                old_size=None if old is None else float(old),
                chunk_key=None if r.get("chunk_key") is None else str(r["chunk_key"]),
                apply_order=(
                    None
                    if r.get("apply_order", payload.get("apply_order")) is None
                    else int(r.get("apply_order", payload.get("apply_order")))
                ),
                source_record_ordinal=(
                    None
                    if r.get("record_ordinal", payload.get("source_record_ordinal"))
                    is None
                    else int(
                        r.get(
                            "record_ordinal",
                            payload.get("source_record_ordinal"),
                        )
                    )
                ),
                record_provenance={
                    k: payload.get(k)
                    for k in (
                        "source_record_id",
                        "message_order",
                        "level_order",
                        "update_id",
                        "seq",
                    )
                    if k in payload
                },
            )
        )
    out.sort(
        key=lambda e: (
            e.event_time_ns,
            e.apply_order if e.apply_order is not None else 0,
            e.side,
            e.price,
        )
    )
    return out


def normalize_trade_rows(rows: list[dict[str, Any]]) -> list[XRayTrade]:
    trades: list[XRayTrade] = []
    for r in rows:
        ts = r["trade_ts"]
        if not isinstance(ts, datetime):
            if "trade_ts_ns" in r:
                ts = ns_to_dt(int(r["trade_ts_ns"]))
            else:
                ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        price = float(r["price"])
        size = float(r["size"])
        trades.append(
            XRayTrade(
                trade_ts=ts,
                trade_id=str(r["trade_id"]),
                side=str(r["side"]),
                price=price,
                size=size,
                notional=float(r.get("notional") or price * size),
            )
        )
    trades.sort(key=lambda t: (t.trade_ts, t.trade_id))
    return trades


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    return float(v)


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


def analysis_query(
    client: QueryClient,
    sql: str,
    *,
    parameters: dict[str, Any] | None = None,
) -> Any:
    assert_select_only_bounded(sql)
    return _analysis_query(client, sql, parameters=parameters)


class LiveSilverMetricsRepository:
    def __init__(
        self,
        client: QueryClient,
        *,
        database: str,
        lock_path=DEFAULT_SILVER_LOCK,
        require_lock_gate: bool = True,
        sentinel: ExecutionSentinel | None = None,
    ):
        self.client = client
        self.database = validate_database_name(database)
        self.lock_path = lock_path
        self.require_lock_gate = require_lock_gate
        self.sentinel = sentinel
        self.calls: list[dict[str, Any]] = []
        self.last_quality: dict[str, Any] = {}

    def _gate(self) -> None:
        if self.sentinel is not None:
            self.sentinel.check("silver_metrics_query")
        elif self.require_lock_gate:
            assert_full_run_allowed(self.lock_path)

    def load_mid_series(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> list[MidState]:
        self._gate()
        if not chunk_keys:
            raise RuntimeError("DATA_SOURCE_UNAVAILABLE:empty_chunk_keys")
        sql = metrics_mid_sql(database=self.database)
        params = {
            "symbol": symbol,
            "chunk_keys": list(chunk_keys),
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
        }
        self.calls.append({"op": "load_mid_series", "sql": sql, "parameters": params})
        raw = analysis_query(self.client, sql, parameters=params)
        series = normalize_mid_rows(_rows_from_result(raw))
        self.last_quality = mid_series_quality(
            series, start_ns=start_ns, end_ns=end_ns
        )
        return series

    def load_book_hash_at_bucket(
        self,
        *,
        symbol: str,
        bucket_start_ns: int,
        chunk_keys: tuple[str, ...],
    ) -> str | None:
        self._gate()
        if not chunk_keys:
            raise RuntimeError("DATA_SOURCE_UNAVAILABLE:empty_chunk_keys")
        sql = metrics_one_bucket_sql(database=self.database)
        params = {
            "symbol": symbol,
            "chunk_keys": list(chunk_keys),
            "bucket_start_ns": int(bucket_start_ns),
        }
        self.calls.append(
            {"op": "load_book_hash_at_bucket", "sql": sql, "parameters": params}
        )
        raw = analysis_query(self.client, sql, parameters=params)
        rows = _rows_from_result(raw)
        if not rows:
            return None
        payload = rows[0].get("payload")
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            return None
        h = payload.get("book_hash")
        return str(h) if h is not None else None


class LiveSilverLevelChangesRepository:
    def __init__(
        self,
        client: QueryClient,
        *,
        database: str,
        local_band_usd: float = 400.0,
        lock_path=DEFAULT_SILVER_LOCK,
        require_lock_gate: bool = True,
        ref_price: float | None = None,
        sentinel: ExecutionSentinel | None = None,
        max_price_span_usd: float = DEFAULT_MAX_LC_PRICE_SPAN_USD,
    ):
        self.client = client
        self.database = validate_database_name(database)
        self.local_band_usd = local_band_usd
        self.lock_path = lock_path
        self.require_lock_gate = require_lock_gate
        self.ref_price = ref_price
        self.sentinel = sentinel
        self.max_price_span_usd = max_price_span_usd
        self.calls: list[dict[str, Any]] = []

    def _gate(self) -> None:
        if self.sentinel is not None:
            self.sentinel.check("silver_level_changes_query")
        elif self.require_lock_gate:
            assert_full_run_allowed(self.lock_path)

    def load_level_changes(
        self,
        *,
        symbol: str,
        start_ns: int,
        end_ns: int,
        chunk_keys: tuple[str, ...],
        price_min: float,
        price_max: float,
    ) -> list[LevelChangeEvent]:
        self._gate()
        if not chunk_keys:
            raise RuntimeError("DATA_SOURCE_UNAVAILABLE:empty_chunk_keys")
        assert_price_band_width(
            price_min, price_max, max_span_usd=self.max_price_span_usd
        )
        sql = level_changes_sql(database=self.database)
        params = {
            "symbol": symbol,
            "chunk_keys": list(chunk_keys),
            "start_ns": int(start_ns),
            "end_ns": int(end_ns),
            "price_min": float(price_min),
            "price_max": float(price_max),
        }
        self.calls.append({"op": "load_level_changes", "sql": sql, "parameters": params})
        raw = analysis_query(self.client, sql, parameters=params)
        # SQL already bands; keep optional client-side band only if ref set historically
        return normalize_level_change_rows(
            _rows_from_result(raw),
            ref_price=None,
            local_band_usd=None,
        )


class LivePublicTradesRepository:
    """Loads raw rows; core owns dedup (public_trade_index contract)."""

    def __init__(
        self,
        client: QueryClient,
        *,
        lock_path=DEFAULT_SILVER_LOCK,
        require_lock_gate: bool = True,
        sentinel: ExecutionSentinel | None = None,
    ):
        self.client = client
        self.lock_path = lock_path
        self.require_lock_gate = require_lock_gate
        self.sentinel = sentinel
        self.calls: list[dict[str, Any]] = []

    def _gate(self) -> None:
        if self.sentinel is not None:
            self.sentinel.check("public_trades_query")
        elif self.require_lock_gate:
            assert_full_run_allowed(self.lock_path)

    def load_trades(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> list[XRayTrade]:
        self._gate()
        sql = public_trades_sql()
        params = {
            "symbol": symbol,
            "start_ts": ns_to_dt(int(start_ns)),
            "end_ts": ns_to_dt(int(end_ns)),
        }
        self.calls.append({"sql": sql, "parameters": params})
        raw = analysis_query(self.client, sql, parameters=params)
        return normalize_trade_rows(_rows_from_result(raw))


class LiveReadinessRepository:
    """Wraps analysis_readiness_v1_3 ledger/gap/assess (fail-closed)."""

    def __init__(
        self,
        client: QueryClient | None = None,
        *,
        build_config: Any | None = None,
        lock_path=DEFAULT_SILVER_LOCK,
        require_lock_gate: bool = True,
        ready_chunks: list[Any] | None = None,
        gap_times: list[int] | None = None,
        chain_version: str = "",
        chain_hash: str = "",
        verify_counts: bool = True,
        sentinel: ExecutionSentinel | None = None,
    ):
        self.client = client
        self.build_config = build_config
        self.lock_path = lock_path
        self.require_lock_gate = require_lock_gate
        self.ready_chunks = ready_chunks
        self.gap_times = gap_times
        self.chain_version = chain_version
        self.chain_hash = chain_hash
        self.verify_counts = verify_counts
        self.sentinel = sentinel
        self.calls: list[str] = []

    def _gate(self) -> None:
        if self.sentinel is not None:
            self.sentinel.check("readiness_assess")
        elif self.require_lock_gate:
            assert_full_run_allowed(self.lock_path)

    def assess_window(
        self, *, symbol: str, start_ns: int, end_ns: int
    ) -> ReadinessResult:
        from obfull_research_engine.clickhouse_research_store_v1 import (
            analysis_readiness_v1_3 as ar,
        )

        try:
            if self.ready_chunks is not None:
                self.calls.append("assess_injected")
                assessment = ar.assess_analysis_window(
                    start_ns=int(start_ns),
                    end_ns=int(end_ns),
                    ready_chunks=self.ready_chunks,
                    gap_times=list(self.gap_times or []),
                )
                epochs_safe = (None, None)
                epoch_id = assessment.epoch_id
            else:
                self._gate()
                if self.client is None or self.build_config is None:
                    return ReadinessResult(
                        status="NOT_READY",
                        reason="DATA_SOURCE_UNAVAILABLE:readiness_client_or_config",
                        chain_version=self.chain_version,
                        chain_hash=self.chain_hash,
                    )
                self.calls.append("load_chunk_ledger")
                ledger = ar.load_chunk_ledger(self.client, self.build_config)
                self.calls.append("classify_chunk")
                ready: list[Any] = []
                for row in ledger:
                    ready.append(
                        ar.classify_chunk(
                            self.client,
                            self.build_config,
                            row,
                            verify_counts=self.verify_counts,
                        )
                    )
                ready = [c for c in ready if c.status == "READY"]
                self.calls.append("load_gap_times")
                gaps = ar.load_gap_times(self.client, self.build_config)
                self.calls.append("assess_analysis_window")
                assessment = ar.assess_analysis_window(
                    start_ns=int(start_ns),
                    end_ns=int(end_ns),
                    ready_chunks=ready,
                    gap_times=gaps,
                )
                epoch_id = assessment.epoch_id
                epochs_safe = (None, None)
                if assessment.status == "READY" and epoch_id:
                    self.calls.append("load_persisted_epochs")
                    epochs = ar.load_persisted_epochs(self.client, self.build_config)
                    for ep in epochs:
                        if ep.epoch_id == epoch_id:
                            epochs_safe = (ep.safe_start_ns, ep.safe_end_ns)
                            break
        except Exception as exc:  # fail-closed
            from ..errors import sanitize_error_message

            return ReadinessResult(
                status="NOT_READY",
                reason=f"READINESS_ERROR:{type(exc).__name__}",
                chain_version=self.chain_version,
                chain_hash=self.chain_hash,
                detail={"error": sanitize_error_message(exc)},
            )

        if assessment.status != "READY":
            return ReadinessResult(
                status="NOT_READY",
                reason=assessment.reason or "NOT_READY",
                chain_version=self.chain_version,
                chain_hash=self.chain_hash,
                detail=_safe_dict(assessment),
            )
        return ReadinessResult(
            status="READY",
            reason=assessment.reason or "READY",
            epoch_id=str(assessment.epoch_id or ""),
            chunk_keys=tuple(assessment.chunk_keys or ()),
            level_change_count=int(assessment.level_change_count or 0),
            state_count=int(assessment.state_count or 0),
            chain_version=self.chain_version
            or getattr(self.build_config, "chain_version", ""),
            chain_hash=self.chain_hash
            or getattr(self.build_config, "expected_chain_hash", ""),
            safe_start_ns=epochs_safe[0],
            safe_end_ns=epochs_safe[1],
            detail=_safe_dict(assessment),
        )


class StubAvrLiveRepository:
    def load_avr(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        return {"status": "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"}


class StubOpenInterestLiveRepository:
    def load_oi(self, *, symbol: str, start_ns: int, end_ns: int) -> dict[str, Any]:
        return {"status": "UNAVAILABLE_ADAPTER_NOT_IMPLEMENTED"}


class LiveMarketProfileRepository:
    def __init__(
        self,
        client: QueryClient | None = None,
        *,
        lock_path=DEFAULT_SILVER_LOCK,
        require_lock_gate: bool = True,
        enabled: bool = True,
        sentinel: ExecutionSentinel | None = None,
    ):
        self.client = client
        self.lock_path = lock_path
        self.require_lock_gate = require_lock_gate
        self.enabled = enabled
        self.sentinel = sentinel
        self.calls: list[dict[str, Any]] = []

    def load_previous_closed_30m_tpo(
        self, *, symbol: str, decision_time: datetime
    ) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("DATA_SOURCE_UNAVAILABLE:market_profile_disabled")
        if self.sentinel is not None:
            self.sentinel.check("market_profile_load")
        elif self.require_lock_gate:
            assert_full_run_allowed(self.lock_path)
        from ..adapters import market_profile as mp

        self.calls.append({"symbol": symbol, "decision_time": decision_time.isoformat()})
        raw = mp.load_strategy_tpo_edge_profile(
            symbol=symbol, decision_time=decision_time, client=self.client
        )
        va = ((raw.get("tpo") or {}).get("value_area")) or {}
        return {
            "window_start": raw["window_start"],
            "window_end": raw["window_end"],
            "profile_window_start": raw["window_start"],
            "profile_window_end": raw["window_end"],
            "known_as_of_utc": raw["window_end"],
            "poc": va.get("poc"),
            "vah": va.get("vah"),
            "val": va.get("val"),
            "tpo": raw.get("tpo"),
            "formula_id": raw.get("formula_id"),
            "implementation_source": raw.get("implementation_source"),
            "raw": raw.get("raw"),
            "temporary_cross_repo_dependency": True,
        }


QUERY_SETTINGS = dict(ANALYSIS_QUERY_SETTINGS)
