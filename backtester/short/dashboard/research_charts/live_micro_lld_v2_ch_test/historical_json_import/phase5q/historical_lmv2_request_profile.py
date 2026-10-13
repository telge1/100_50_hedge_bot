"""Optional request profiler for historical static timeline (off unless enabled).

Enable: HISTORICAL_LMV2_PROFILE=1 or profile_context() in phase5q scripts only.
Does not alter pool semantics; wraps ClickHouse client.query and times pipeline stages.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import resource
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

_PROFILE_ENABLED = contextvars.ContextVar("historical_lmv2_profile_enabled", default=False)


@dataclass
class HistoricalLmv2ProfileReport:
    request_total_ms: float = 0.0
    clickhouse_total_ms: float = 0.0
    clickhouse_query_count: int = 0
    clickhouse_rows_read: int = 0
    clickhouse_bytes_read: int = 0
    clickhouse_rows_returned: int = 0
    active_reader_ms: float = 0.0
    membership_reconstruction_ms: float = 0.0
    episode_walk_ms: float = 0.0
    episode_merge_ms: float = 0.0
    geometry_clip_ms: float = 0.0
    json_serialization_ms: float = 0.0
    response_bytes: int = 0
    peak_rss_mb: float = 0.0
    display_close_count: int = 0
    episode_walk_close_count: int = 0
    pool_id_count: int = 0
    membership_entry_count: int = 0
    episode_count: int = 0
    segment_count: int = 0
    query_samples: list[dict[str, Any]] = field(default_factory=list)
    timing_notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: (v if v is not None else "NOT_MEASURED") for k, v in self.__dict__.items()}


_REPORT = HistoricalLmv2ProfileReport()
_LOCK = threading.Lock()
_ORIG_GET_CLIENT: Callable[..., Any] | None = None
_PATCHED = False


def profile_enabled() -> bool:
    return bool(_PROFILE_ENABLED.get()) or os.environ.get("HISTORICAL_LMV2_PROFILE") == "1"


@contextlib.contextmanager
def profile_context():
    token = _PROFILE_ENABLED.set(True)
    global _REPORT
    with _LOCK:
        _REPORT = HistoricalLmv2ProfileReport()
    try:
        yield _REPORT
    finally:
        _PROFILE_ENABLED.reset(token)


def get_report() -> HistoricalLmv2ProfileReport:
    return _REPORT


def record_stage_ms(name: str, elapsed_ms: float) -> None:
    if not profile_enabled():
        return
    r = _REPORT
    if name == "active_reader":
        r.active_reader_ms += elapsed_ms
    elif name == "membership_reconstruction":
        r.membership_reconstruction_ms += elapsed_ms
    elif name == "episode_walk":
        r.episode_walk_ms += elapsed_ms
    elif name == "episode_merge":
        r.episode_merge_ms += elapsed_ms
    elif name == "geometry_clip":
        r.geometry_clip_ms += elapsed_ms
    elif name == "json_serialization":
        r.json_serialization_ms += elapsed_ms


def _normalize_sql(sql: str) -> str:
    s = " ".join(str(sql or "").split())
    return s[:500]


def _wrap_query_result(result: Any) -> tuple[int, int, int]:
    rows_ret = 0
    rows_read = 0
    bytes_read = 0
    try:
        if hasattr(result, "result_rows"):
            rows_ret = len(result.result_rows or [])
        if hasattr(result, "summary"):
            summ = result.summary or {}
            rows_read = int(summ.get("read_rows") or summ.get("rows_read") or 0)
            bytes_read = int(summ.get("read_bytes") or summ.get("bytes_read") or 0)
    except Exception:
        pass
    if rows_ret and not rows_read:
        rows_read = rows_ret
    return rows_ret, rows_read, bytes_read


def install_clickhouse_query_instrumentation() -> None:
    global _PATCHED, _ORIG_GET_CLIENT
    if _PATCHED:
        return
    import clickhouse_connect

    _ORIG_GET_CLIENT = clickhouse_connect.get_client

    class _InstrumentedClient:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def query(self, sql: str, *args, **kwargs):
            if not profile_enabled():
                return self._inner.query(sql, *args, **kwargs)
            t0 = time.perf_counter()
            result = self._inner.query(sql, *args, **kwargs)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            rows_ret, rows_read, bytes_read = _wrap_query_result(result)
            with _LOCK:
                r = _REPORT
                r.clickhouse_total_ms += elapsed_ms
                r.clickhouse_query_count += 1
                r.clickhouse_rows_returned += rows_ret
                r.clickhouse_rows_read += rows_read
                r.clickhouse_bytes_read += bytes_read
                if len(r.query_samples) < 12:
                    r.query_samples.append(
                        {
                            "elapsed_ms": round(elapsed_ms, 3),
                            "rows_returned": rows_ret,
                            "rows_read": rows_read,
                            "bytes_read": bytes_read,
                            "sql_prefix": _normalize_sql(sql),
                        }
                    )
            return result

        def close(self, *args, **kwargs):
            return self._inner.close(*args, **kwargs)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    def _get_client_wrapped(*args, **kwargs):
        return _InstrumentedClient(_ORIG_GET_CLIENT(*args, **kwargs))

    clickhouse_connect.get_client = _get_client_wrapped
    _PATCHED = True


def patch_published_reader_load_published() -> None:
    from research_charts.live_micro_lld_v2_ch_test.active_snapshot import published_reader

    if getattr(published_reader.PublishedActiveReader, "_phase5q_patched", False):
        return
    orig = published_reader.PublishedActiveReader.load_published

    def _wrapped(self, pipeline_line_id, decision_close, *, config_hash):
        if not profile_enabled():
            return orig(self, pipeline_line_id, decision_close, config_hash=config_hash)
        t0 = time.perf_counter()
        out = orig(self, pipeline_line_id, decision_close, config_hash=config_hash)
        record_stage_ms("active_reader", (time.perf_counter() - t0) * 1000.0)
        return out

    published_reader.PublishedActiveReader.load_published = _wrapped
    published_reader.PublishedActiveReader._phase5q_patched = True


def patch_episode_pipeline() -> None:
    import research_charts.historical_pool_state_v1 as hps

    if getattr(hps, "_phase5q_patched", False):
        return

    orig_build = hps.build_active_episode_records
    orig_catalog = hps._load_catalog
    orig_zones = hps.active_episode_domain_zones

    def _load_catalog_wrapped(client, database, snapshot_id, decision_close):
        if not profile_enabled():
            return orig_catalog(client, database, snapshot_id, decision_close)
        t0 = time.perf_counter()
        out = orig_catalog(client, database, snapshot_id, decision_close)
        record_stage_ms("membership_reconstruction", (time.perf_counter() - t0) * 1000.0)
        with _LOCK:
            _REPORT.membership_entry_count += len(out)
        return out

    def _build_wrapped(reader, cfg_hash, client, database, closes):
        if not profile_enabled():
            return orig_build(reader, cfg_hash, client, database, closes)
        t0 = time.perf_counter()
        out = orig_build(reader, cfg_hash, client, database, closes)
        record_stage_ms("episode_walk", (time.perf_counter() - t0) * 1000.0)
        with _LOCK:
            _REPORT.episode_walk_close_count = len(closes)
            _REPORT.episode_count = len(out)
            _REPORT.pool_id_count = len({p["pool_id"] for p in out})
        return out

    def _zones_wrapped(pools, *, from_unix, to_unix, layer, namespace):
        if not profile_enabled():
            return orig_zones(pools, from_unix=from_unix, to_unix=to_unix, layer=layer, namespace=namespace)
        t0 = time.perf_counter()
        out = orig_zones(
            pools, from_unix=from_unix, to_unix=to_unix, layer=layer, namespace=namespace
        )
        record_stage_ms("geometry_clip", (time.perf_counter() - t0) * 1000.0)
        with _LOCK:
            _REPORT.segment_count = len(out)
        return out

    hps.build_active_episode_records = _build_wrapped
    hps._load_catalog = _load_catalog_wrapped
    hps.active_episode_domain_zones = _zones_wrapped
    hps._phase5q_patched = True


def measure_peak_rss_mb() -> float:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return round(usage.ru_maxrss / 1024.0, 2)  # Linux KB → MB
    except Exception:
        return -1.0


def finalize_payload_metrics(payload: dict[str, Any], total_ms: float) -> None:
    if not profile_enabled():
        return
    t0 = time.perf_counter()
    raw = json.dumps(payload, separators=(",", ":"), default=str)
    record_stage_ms("json_serialization", (time.perf_counter() - t0) * 1000.0)
    with _LOCK:
        r = _REPORT
        r.request_total_ms = total_ms
        r.response_bytes = len(raw.encode("utf-8"))
        r.display_close_count = int(payload.get("pilot_close_count") or 0)
        r.peak_rss_mb = measure_peak_rss_mb()
        exclusive = (
            r.active_reader_ms
            + r.membership_reconstruction_ms
            + r.geometry_clip_ms
            + r.json_serialization_ms
        )
        if r.episode_walk_ms > exclusive:
            r.timing_notes.append(
                "episode_walk_ms is inclusive of active_reader + membership_reconstruction "
                "(nested patches); do not sum all stage fields for total."
            )
        if r.clickhouse_total_ms > r.request_total_ms * 1.05:
            r.timing_notes.append(
                "clickhouse_total_ms can exceed wall clock when queries run in nested clients "
                "sequentially within one thread (still valid CH time)."
            )
