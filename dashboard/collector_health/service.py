"""Assemble evidence-based collector health (cached, timeouts)."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from . import (
    OI_GRANULARITY,
    OI_SOT_DATABASE,
    OI_SOT_TABLE,
    OI_SOURCE,
    PUBLIC_TRADES_BACKFILL_GATE,
    PUBLIC_TRADES_UI_BANNER,
)
from .ch_config import load_orderbook_ch_config
from .contract import THRESHOLDS, empty_collector, sanitize_json, utc_now
from .oi_backfill import last_closed_5m
from .probes import probe_full_ob_raw, probe_oi_process, probe_stoch_process, probe_stoch_status

logger = logging.getLogger(__name__)

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "payload": None}


def _age_seconds(ts: datetime | None, *, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    n = now or utc_now()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (n - ts.astimezone(timezone.utc)).total_seconds())


def _ch_query(sql: str, parameters: dict | None = None) -> list[tuple]:
    import clickhouse_connect

    cfg = load_orderbook_ch_config()
    client = clickhouse_connect.get_client(
        **cfg.connect_kwargs(),
        connect_timeout=THRESHOLDS["db_query_timeout_s"],
        send_receive_timeout=THRESHOLDS["db_query_timeout_s"],
    )
    try:
        return list(client.query(sql, parameters=parameters or {}).result_rows)
    finally:
        client.close()


def _build_full_ob() -> dict[str, Any]:
    c = empty_collector(
        "full_ob_raw_archive",
        display_name="Full Orderbook Raw Archive",
        status="STOPPED",
        evidence="process absent; Phase B forbids start/repair",
    )
    probe = probe_full_ob_raw()
    c["process_running"] = probe["process_running"]
    c["pid"] = probe["pid"]
    c["process_started_at"] = probe["process_started_at"]
    c["source_connected"] = probe.get("connected")
    c["last_error"] = probe.get("last_error")
    c["writer_status"] = "DISABLED_OR_STOPPED"
    c["backfill_supported"] = False
    c["backfill_status"] = "DISABLED"
    c["granularity"] = "raw_orderbook"
    c["source"] = "bybit_ws_orderbook"
    if probe["process_running"]:
        c["status"] = "DEGRADED"
        c["evidence"] = "unexpected process running; no restart/kill from this module"
        c["reason"] = c["evidence"]
    else:
        c["status"] = "STOPPED"
        c["evidence"] = f"STOPPED health_state={probe.get('health_state')!r}"
        c["reason"] = c["evidence"]
    return c


def _build_oi_live() -> dict[str, Any]:
    c = empty_collector(
        "oi_liquidation_live",
        display_name="OI + Liquidations (live WS)",
        status="UNKNOWN",
    )
    proc = probe_oi_process()
    c["process_running"] = proc["process_running"]
    c["pid"] = proc["pid"]
    c["process_started_at"] = proc["process_started_at"]
    c["expected_symbol_count"] = 51
    c["granularity"] = "5s_events"
    c["source"] = "BYBIT_WS_REALTIME"
    c["backfill_supported"] = False
    c["backfill_status"] = "N/A_LIVE_STREAM"
    try:
        rows = _ch_query(
            """
            SELECT
              (SELECT max(bucket_time) FROM open_interest_5s),
              (SELECT max(inserted_at) FROM open_interest_5s),
              (SELECT max(event_ts) FROM oi_liquidation_health),
              (SELECT max(event_time) FROM all_liquidations),
              (SELECT max(received_at) FROM all_liquidations)
            """
        )
        oi_max, oi_ins, health_max, liq_max, liq_recv = rows[0]
        c["latest_exchange_timestamp"] = oi_max
        c["latest_ingest_timestamp"] = oi_ins
        c["last_successful_write_at"] = oi_ins
        c["last_source_message_at"] = health_max
        lag = _age_seconds(health_max if health_max else oi_max)
        c["lag_seconds"] = lag
        c["persistence_lag_seconds"] = _age_seconds(oi_ins)
        # Liquidations sparse: use health heartbeat
        stale_lim = THRESHOLDS["oi_live_heartbeat_stale_s"]
        if not proc["process_running"]:
            c["status"] = "STOPPED"
            c["evidence"] = "OI/Liq process not running"
        elif lag is None or lag > max(3600.0, stale_lim):
            c["status"] = "STALE"
            c["source_connected"] = False
            c["writer_status"] = "STALE_OR_FAILED"
            c["evidence"] = (
                f"process alive but health/DB frozen; oi5s_max={oi_max}; "
                f"health_max={health_max}; liq_max={liq_max}; lag_s={lag}"
            )
            c["last_error"] = "db_heartbeat_stale"
        elif lag > stale_lim:
            c["status"] = "STALE"
            c["source_connected"] = False
            c["evidence"] = f"heartbeat lag {lag:.0f}s > {stale_lim}s"
        else:
            c["status"] = "HEALTHY"
            c["source_connected"] = True
            c["writer_status"] = "OK"
            c["evidence"] = "process + fresh health/OI5s"
        c["coverage_status"] = "LIVE_STREAM_SEPARATE_FROM_5M_HISTORY"
        # note liq
        c["reason"] = c["evidence"]
    except Exception as exc:
        logger.warning("oi live db probe failed: %s", exc)
        c["status"] = "UNKNOWN" if proc["process_running"] else "STOPPED"
        c["last_error"] = str(exc)[:300]
        c["evidence"] = "db_timeout_or_error"
        c["reason"] = c["evidence"]
    return c


def _build_oi_5m() -> dict[str, Any]:
    c = empty_collector(
        "oi_5m_history",
        display_name="OI 5m History (REST SoT)",
        status="UNKNOWN",
    )
    c["backfill_supported"] = True
    c["backfill_status"] = "READY_DETECT_DRY_RUN"
    c["source"] = OI_SOURCE
    c["granularity"] = OI_GRANULARITY
    c["process_running"] = False
    c["writer_status"] = "BATCH_CLI"
    try:
        rows = _ch_query(
            """
            SELECT
              count(),
              uniqExact(symbol),
              min(bucket_time),
              max(bucket_time),
              max(inserted_at)
            FROM open_interest_5m_history
            WHERE source = {source:String}
            """,
            {"source": OI_SOURCE},
        )
        n, nsym, mn, mx, ins = rows[0]
        c["latest_exchange_timestamp"] = mx
        c["latest_ingest_timestamp"] = ins
        c["last_successful_write_at"] = ins
        c["fresh_symbol_count"] = int(nsym or 0)
        c["expected_symbol_count"] = None
        closed = last_closed_5m()
        # Gap from max+5m to last closed for BTC if present
        gap_count = None
        if mx is not None:
            mx_aware = mx if getattr(mx, "tzinfo", None) else mx.replace(tzinfo=timezone.utc)
            start = mx_aware.astimezone(timezone.utc)
            from datetime import timedelta

            gap_start = start + timedelta(minutes=5)
            if gap_start <= closed:
                gap_count = int((closed - gap_start).total_seconds() // 300) + 1
            else:
                gap_count = 0
        c["gap_count"] = gap_count
        c["coverage_status"] = (
            "PARTIAL"
            if (nsym or 0) < 2 or (gap_count or 0) > 0
            else "COMPLETE_KNOWN_SPAN"
        )
        if (gap_count or 0) > 0:
            c["status"] = "DEGRADED"
            c["evidence"] = (
                f"SoT {OI_SOT_DATABASE}.{OI_SOT_TABLE} rows={n} symbols={nsym} "
                f"max={mx} missing_closed≈{gap_count}; 5s history must not be inferred"
            )
        else:
            c["status"] = "HEALTHY"
            c["evidence"] = f"rows={n} symbols={nsym} max={mx}"
        c["reason"] = c["evidence"]
    except Exception as exc:
        c["status"] = "UNKNOWN"
        c["last_error"] = str(exc)[:300]
        c["evidence"] = "db_timeout_or_error"
        c["reason"] = c["evidence"]
    return c


def _build_public_trades() -> dict[str, Any]:
    c = empty_collector(
        "public_trades_live",
        display_name="Public Trades (live)",
        status="UNKNOWN",
    )
    proc = probe_stoch_process()
    api = probe_stoch_status(timeout_s=THRESHOLDS["http_timeout_s"])
    c["process_running"] = proc["process_running"]
    c["pid"] = proc["pid"]
    c["process_started_at"] = proc["process_started_at"]
    c["backfill_supported"] = False
    c["backfill_status"] = f"DISABLED_GATE={PUBLIC_TRADES_BACKFILL_GATE}"
    c["granularity"] = "tick"
    c["source"] = "bybit_ws_publicTrade"
    c["expected_symbol_count"] = 51
    c["coverage_status"] = PUBLIC_TRADES_UI_BANNER
    data = api.get("data") if api.get("ok") else None
    if data:
        c["source_connected"] = bool(data.get("websocket_connected"))
        c["last_source_message_at"] = data.get("last_message_at")
        pt = data.get("public_trade_metrics") or {}
        c["latest_exchange_timestamp"] = pt.get("last_trade_event_ts")
        c["latest_ingest_timestamp"] = pt.get("last_trade_ingest_ts")
        c["last_successful_write_at"] = pt.get("last_trade_ingest_ts")
        c["lag_seconds"] = pt.get("lag_seconds")
        c["queue_depth"] = pt.get("queue_depth")
        c["dropped_events"] = pt.get("dropped_events")
        c["reconnect_count"] = pt.get("reconnect_count")
        c["last_error"] = pt.get("last_error")
        c["fresh_symbol_count"] = len(data.get("public_trade_symbols") or [])
        drops = int(pt.get("dropped_events") or 0)
        lag = pt.get("lag_seconds")
        insert_failures = int(pt.get("insert_failures") or 0)
        if not proc["process_running"]:
            c["status"] = "STOPPED"
            c["evidence"] = "process missing"
        elif not data.get("public_trades_enabled"):
            c["status"] = "DEGRADED"
            c["evidence"] = "public trades disabled"
        elif drops > 0 or insert_failures > 0:
            c["status"] = "DEGRADED"
            c["writer_status"] = "OK_WITH_LIFETIME_DROPS" if insert_failures == 0 else "WRITE_ERRORS"
            c["evidence"] = (
                f"{PUBLIC_TRADES_UI_BANNER}; dropped_events={drops}; "
                f"lag={lag}; max_ts alone is not completeness"
            )
        elif lag is not None and float(lag) > THRESHOLDS["public_trades_lag_stale_s"]:
            c["status"] = "STALE"
            c["evidence"] = f"lag {lag}s"
        elif lag is not None and float(lag) > THRESHOLDS["public_trades_lag_warn_s"]:
            c["status"] = "DEGRADED"
            c["evidence"] = f"lag warn {lag}s"
        else:
            # Still DEGRADED until drop root-cause cleared per Phase B gate
            c["status"] = "DEGRADED"
            c["writer_status"] = "OK"
            c["evidence"] = (
                f"{PUBLIC_TRADES_UI_BANNER}; gate={PUBLIC_TRADES_BACKFILL_GATE}"
            )
        c["reason"] = c["evidence"]
    else:
        try:
            rows = _ch_query(
                """
                SELECT max(trade_ts), max(ingest_timestamp),
                       uniqExactIf(symbol, trade_ts > now() - INTERVAL 10 MINUTE)
                FROM public_trades_canonical
                """
            )
            mx, ins, nsym = rows[0]
            c["latest_exchange_timestamp"] = mx
            c["latest_ingest_timestamp"] = ins
            c["fresh_symbol_count"] = int(nsym or 0)
            lag = _age_seconds(mx)
            c["lag_seconds"] = lag
            if not proc["process_running"]:
                c["status"] = "STOPPED"
            elif lag is not None and lag < THRESHOLDS["public_trades_lag_stale_s"]:
                c["status"] = "DEGRADED"
                c["evidence"] = f"{PUBLIC_TRADES_UI_BANNER}; api unreachable; db lag={lag}"
            else:
                c["status"] = "STALE"
                c["evidence"] = "api unreachable and db stale"
            c["last_error"] = api.get("error")
            c["reason"] = c["evidence"]
        except Exception as exc:
            c["status"] = "UNKNOWN"
            c["last_error"] = str(exc)[:300]
            c["evidence"] = "api_and_db_unavailable"
            c["reason"] = c["evidence"]
    return c


def _build_candles() -> dict[str, Any]:
    c = empty_collector(
        "candles_1m_live",
        display_name="Candles 1m (live+recovery)",
        status="UNKNOWN",
    )
    proc = probe_stoch_process()
    api = probe_stoch_status(timeout_s=THRESHOLDS["http_timeout_s"])
    c["process_running"] = proc["process_running"]
    c["pid"] = proc["pid"]
    c["process_started_at"] = proc.get("process_started_at")
    c["backfill_supported"] = False
    c["backfill_status"] = "STARTUP_CANDLE_RECOVERY_ONLY"
    c["granularity"] = "1m"
    c["source"] = "bybit_ws_kline+rest_recovery"
    data = api.get("data") if api.get("ok") else None
    if not proc["process_running"]:
        c["status"] = "STOPPED"
        c["evidence"] = "stoch process missing"
    elif data:
        c["source_connected"] = bool(data.get("websocket_connected"))
        c["last_source_message_at"] = data.get("last_message_at")
        c["reconnect_count"] = data.get("reconnect_count")
        c["last_error"] = data.get("last_error")
        stale = data.get("stale_candle_symbols") or []
        c["stale_symbols"] = list(stale)[:20]
        c["expected_symbol_count"] = data.get("symbols_total") or data.get("configured_count")
        c["fresh_symbol_count"] = data.get("live_count") or data.get("symbols_live")
        state = str(data.get("state") or data.get("collector_state") or "").upper()
        if state == "LIVE" and not stale:
            c["status"] = "HEALTHY"
            c["writer_status"] = "OK"
            c["evidence"] = "LIVE; candle recovery separate from public trades"
        elif state == "LIVE":
            c["status"] = "DEGRADED"
            c["evidence"] = f"LIVE with stale_candle_symbols={len(stale)}"
        else:
            c["status"] = "DEGRADED"
            c["evidence"] = f"state={state}"
        c["reason"] = c["evidence"]
    else:
        c["status"] = "UNKNOWN"
        c["evidence"] = api.get("error") or "status api unreachable"
        c["reason"] = c["evidence"]
    return c


def build_health_report(*, use_cache: bool = True) -> dict[str, Any]:
    now = time.monotonic()
    with _cache_lock:
        if (
            use_cache
            and _cache["payload"] is not None
            and now - float(_cache["ts"]) < THRESHOLDS["health_cache_ttl_s"]
        ):
            return sanitize_json(_cache["payload"])

    collectors = [
        _build_full_ob(),
        _build_oi_live(),
        _build_oi_5m(),
        _build_public_trades(),
        _build_candles(),
    ]
    payload = {
        "contract_version": "collector_health_v1",
        "checked_at": utc_now().isoformat().replace("+00:00", "Z"),
        "oi_sot": {
            "database": OI_SOT_DATABASE,
            "table": OI_SOT_TABLE,
            "source": OI_SOURCE,
            "granularity": OI_GRANULARITY,
            "note": "MySQL research_open_interest_5m is not this SoT",
        },
        "gates": {
            "public_trades_backfill": PUBLIC_TRADES_BACKFILL_GATE,
            "public_trades_ui": PUBLIC_TRADES_UI_BANNER,
            "oi_backfill_execute_default": "dry_run_only_until_activation",
            "full_ob_restart": "FORBIDDEN_THIS_PHASE",
        },
        "collectors": collectors,
    }
    payload = sanitize_json(payload)
    with _cache_lock:
        _cache["ts"] = now
        _cache["payload"] = payload
    return payload


def get_collector(collector_id: str) -> dict[str, Any] | None:
    report = build_health_report()
    for c in report["collectors"]:
        if c["collector_id"] == collector_id:
            return c
    return None
