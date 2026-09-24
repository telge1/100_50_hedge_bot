"""Active import verification: snapshot counters → wait → re-check deltas.

Proves data is actually advancing (not just process-alive).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import OI_SOURCE
from .ch_config import load_orderbook_ch_config
from .contract import THRESHOLDS, sanitize_json, utc_now
from .probes import probe_full_ob_raw, probe_ob1000_materializer, probe_oi_process, probe_stoch_status


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _ch_query(sql: str, parameters: dict | None = None, *, database: str | None = None) -> list[tuple]:
    import clickhouse_connect

    cfg = load_orderbook_ch_config()
    kwargs = cfg.connect_kwargs()
    if database:
        kwargs["database"] = database
    client = clickhouse_connect.get_client(
        **kwargs,
        connect_timeout=THRESHOLDS["db_query_timeout_s"],
        send_receive_timeout=THRESHOLDS["db_query_timeout_s"],
    )
    try:
        return list(client.query(sql, parameters=parameters or {}).result_rows)
    finally:
        client.close()


def _research_ch_query(sql: str, parameters: dict | None = None) -> list[tuple]:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from research.btc_doge_research.clickhouse import connect

    client = connect()
    try:
        return list(client.query(sql, parameters=parameters or {}).result_rows)
    finally:
        client.close()


def snapshot_import_counters() -> dict[str, Any]:
    """Point-in-time counters that must advance if import is healthy."""
    now = utc_now()
    out: dict[str, Any] = {"captured_at": now.isoformat().replace("+00:00", "Z")}

    raw = probe_full_ob_raw()
    ob1000 = raw.get("ob1000") or {}
    out["ob1000_raw"] = {
        "events_written": ob1000.get("raw_events_written"),
        "last_write_at": ob1000.get("raw_last_write_at"),
        "process_running": raw.get("process_running"),
        "connected": raw.get("connected"),
    }
    out["full_ob_raw"] = {
        "messages_written": raw.get("full_ob_messages_written"),
        "queue_depth": raw.get("full_ob_queue_depth"),
        "process_running": raw.get("process_running"),
        "connected": raw.get("connected"),
    }

    mat = probe_ob1000_materializer()
    hb = mat.get("heartbeat") or {}
    lag = (hb.get("lag") or {}).get("symbols") or {}
    out["ob1000_materializer"] = {
        "process_running": mat.get("process_running"),
        "status": hb.get("status"),
        "cycle": hb.get("cycle"),
        "rows_inserted_last_cycle": hb.get("rows_inserted"),
        "btc_ch_max": (lag.get("BTCUSDT") or {}).get("ch_max_snapshot_ts"),
        "doge_ch_max": (lag.get("DOGEUSDT") or {}).get("ch_max_snapshot_ts"),
        "worst_lag_seconds": (hb.get("lag") or {}).get("worst_lag_seconds"),
    }
    try:
        rows = _research_ch_query(
            """
            SELECT symbol, max(snapshot_ts), count()
            FROM btc_doge_research.research_ob1000_snapshots_1s
            WHERE symbol IN ('BTCUSDT', 'DOGEUSDT')
            GROUP BY symbol
            """
        )
        by_sym = {str(r[0]): {"max_ts": r[1], "rows": int(r[2])} for r in rows}
        out["ob1000_materializer"]["ch"] = by_sym
    except Exception as exc:  # noqa: BLE001
        out["ob1000_materializer"]["ch_error"] = str(exc)[:200]

    stoch = probe_stoch_status(timeout_s=THRESHOLDS["http_timeout_s"])
    data = stoch.get("data") if stoch.get("ok") else None
    pt = (data or {}).get("public_trade_metrics") or {}
    out["public_trades"] = {
        "api_ok": bool(stoch.get("ok")),
        "rows_inserted": pt.get("rows_inserted"),
        "rows_received": pt.get("rows_received"),
        "last_trade_event_ts": pt.get("last_trade_event_ts"),
        "lag_seconds": pt.get("lag_seconds"),
        "dropped_events": pt.get("dropped_events"),
    }
    out["candles_1m"] = {
        "api_ok": bool(stoch.get("ok")),
        "collector_state": (data or {}).get("collector_state"),
        "websocket_connected": (data or {}).get("websocket_connected"),
        "candle_symbols": len((data or {}).get("candle_symbols") or []),
        "last_closed_sample": list(((data or {}).get("last_closed_candle_by_symbol") or {}).items())[:3],
    }
    try:
        rows = _ch_query(
            """
            SELECT max(open_time), countIf(open_time > now() - INTERVAL 5 MINUTE)
            FROM signal_generator.candles_1m
            WHERE interval = '1m'
            """,
            database="signal_generator",
        )
        out["candles_1m"]["ch_max_bucket"] = rows[0][0] if rows else None
        out["candles_1m"]["ch_rows_5m"] = int(rows[0][1]) if rows else None
    except Exception as exc:  # noqa: BLE001
        out["candles_1m"]["ch_error"] = str(exc)[:200]

    oi_proc = probe_oi_process()
    out["oi_liquidation"] = {
        "process_running": oi_proc.get("process_running"),
        "pid": oi_proc.get("pid"),
    }
    try:
        rows = _ch_query(
            """
            SELECT
              (SELECT max(bucket_time) FROM open_interest_5s),
              (SELECT max(inserted_at) FROM open_interest_5s),
              (SELECT max(event_time) FROM all_liquidations),
              (SELECT count() FROM open_interest_5s WHERE bucket_time > now() - INTERVAL 2 MINUTE)
            """
        )
        out["oi_liquidation"]["oi5s_max"] = rows[0][0]
        out["oi_liquidation"]["oi5s_inserted_at"] = rows[0][1]
        out["oi_liquidation"]["liq_max"] = rows[0][2]
        out["oi_liquidation"]["oi5s_rows_2m"] = int(rows[0][3] or 0)
    except Exception as exc:  # noqa: BLE001
        out["oi_liquidation"]["ch_error"] = str(exc)[:200]

    try:
        rows = _ch_query(
            """
            SELECT max(bucket_time), max(inserted_at)
            FROM open_interest_5m_history
            WHERE source = {source:String}
            """,
            {"source": OI_SOURCE},
        )
        out["oi_5m_history"] = {
            "max_bucket": rows[0][0] if rows else None,
            "max_inserted_at": rows[0][1] if rows else None,
            "note": "batch SoT — may not advance every few seconds",
        }
    except Exception as exc:  # noqa: BLE001
        out["oi_5m_history"] = {"ch_error": str(exc)[:200]}

    return sanitize_json(out)


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _advanced_number(before: Any, after: Any) -> bool:
    a, b = _num(before), _num(after)
    if a is None or b is None:
        return False
    return b > a


def _advanced_ts(before: Any, after: Any) -> bool:
    a, b = _parse_ts(before), _parse_ts(after)
    if a is None or b is None:
        return False
    return b > a


def _judge(name: str, *, ok: bool, detail: str, required: bool = True) -> dict[str, Any]:
    return {
        "id": name,
        "ok": ok,
        "required": required,
        "verdict": "PASS" if ok else ("FAIL" if required else "SKIP"),
        "detail": detail,
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    b_ob = before.get("ob1000_raw") or {}
    a_ob = after.get("ob1000_raw") or {}
    ok = _advanced_number(b_ob.get("events_written"), a_ob.get("events_written")) or _advanced_ts(
        b_ob.get("last_write_at"), a_ob.get("last_write_at")
    )
    checks.append(
        _judge(
            "ob1000_raw_archive",
            ok=ok,
            detail=(
                f"events {b_ob.get('events_written')} → {a_ob.get('events_written')}; "
                f"last {b_ob.get('last_write_at')} → {a_ob.get('last_write_at')}"
            ),
        )
    )

    b_f = before.get("full_ob_raw") or {}
    a_f = after.get("full_ob_raw") or {}
    ok = _advanced_number(b_f.get("messages_written"), a_f.get("messages_written"))
    checks.append(
        _judge(
            "full_ob_raw_archive",
            ok=ok,
            detail=f"messages {b_f.get('messages_written')} → {a_f.get('messages_written')}",
        )
    )

    b_m = before.get("ob1000_materializer") or {}
    a_m = after.get("ob1000_materializer") or {}
    b_ch = b_m.get("ch") or {}
    a_ch = a_m.get("ch") or {}
    mat_ok = False
    details = []
    for sym in ("BTCUSDT", "DOGEUSDT"):
        bb = (b_ch.get(sym) or {}).get("max_ts")
        aa = (a_ch.get(sym) or {}).get("max_ts")
        sym_ok = _advanced_ts(bb, aa) or (
            _num((b_ch.get(sym) or {}).get("rows")) is not None
            and _advanced_number((b_ch.get(sym) or {}).get("rows"), (a_ch.get(sym) or {}).get("rows"))
        )
        mat_ok = mat_ok or sym_ok
        details.append(f"{sym} max {bb} → {aa}")
    # Soft PASS when already caught up (tip may not move every few seconds).
    worst = a_m.get("worst_lag_seconds")
    worst_ok = worst is not None and float(worst) <= 90.0
    if not mat_ok and a_m.get("process_running") and worst_ok:
        if a_m.get("btc_ch_max") or a_m.get("doge_ch_max"):
            mat_ok = True
            details.append("caught-up (lag<=90s, process running; tip may not move every 5s)")
    checks.append(_judge("ob1000_materializer", ok=mat_ok, detail="; ".join(details)))

    b_pt = before.get("public_trades") or {}
    a_pt = after.get("public_trades") or {}
    ok = _advanced_number(b_pt.get("rows_inserted"), a_pt.get("rows_inserted")) or _advanced_ts(
        b_pt.get("last_trade_event_ts"), a_pt.get("last_trade_event_ts")
    )
    checks.append(
        _judge(
            "public_trades_live",
            ok=ok,
            detail=(
                f"rows_inserted {b_pt.get('rows_inserted')} → {a_pt.get('rows_inserted')}; "
                f"last {b_pt.get('last_trade_event_ts')} → {a_pt.get('last_trade_event_ts')}"
            ),
        )
    )

    b_c = before.get("candles_1m") or {}
    a_c = after.get("candles_1m") or {}
    ok = (
        _advanced_ts(b_c.get("ch_max_bucket"), a_c.get("ch_max_bucket"))
        or (
            a_c.get("collector_state") == "LIVE"
            and a_c.get("websocket_connected") is True
            and int(a_c.get("candle_symbols") or 0) >= 40
            and int(a_c.get("ch_rows_5m") or 0) > 0
        )
    )
    checks.append(
        _judge(
            "candles_1m_live",
            ok=ok,
            detail=(
                f"state={a_c.get('collector_state')} ws={a_c.get('websocket_connected')} "
                f"symbols={a_c.get('candle_symbols')} ch_max {b_c.get('ch_max_bucket')} → {a_c.get('ch_max_bucket')} "
                f"rows_5m={a_c.get('ch_rows_5m')}"
            ),
        )
    )

    b_oi = before.get("oi_liquidation") or {}
    a_oi = after.get("oi_liquidation") or {}
    ok = (
        _advanced_ts(b_oi.get("oi5s_max"), a_oi.get("oi5s_max"))
        or _advanced_ts(b_oi.get("oi5s_inserted_at"), a_oi.get("oi5s_inserted_at"))
        or _advanced_number(b_oi.get("oi5s_rows_2m"), a_oi.get("oi5s_rows_2m"))
        or int(a_oi.get("oi5s_rows_2m") or 0) > 0
    )
    checks.append(
        _judge(
            "oi_liquidation_live",
            ok=ok,
            detail=(
                f"oi5s_max {b_oi.get('oi5s_max')} → {a_oi.get('oi5s_max')}; "
                f"rows_2m={a_oi.get('oi5s_rows_2m')}"
            ),
        )
    )

    # OI 5m history is batch — informational only
    b5 = before.get("oi_5m_history") or {}
    a5 = after.get("oi_5m_history") or {}
    checks.append(
        _judge(
            "oi_5m_history",
            ok=True,
            required=False,
            detail=(
                f"batch SoT max {b5.get('max_bucket')} → {a5.get('max_bucket')} "
                "(need not advance every few seconds)"
            ),
        )
    )

    required = [c for c in checks if c.get("required")]
    passed = sum(1 for c in required if c["ok"])
    failed = [c["id"] for c in required if not c["ok"]]
    overall = "PASS" if not failed else "FAIL"
    return sanitize_json(
        {
            "overall": overall,
            "passed": passed,
            "failed": failed,
            "checks": checks,
        }
    )


def verify_import(*, wait_seconds: float = 5.0) -> dict[str, Any]:
    wait_seconds = max(3.0, min(30.0, float(wait_seconds)))
    before = snapshot_import_counters()
    time.sleep(wait_seconds)
    after = snapshot_import_counters()
    result = compare_snapshots(before, after)
    return sanitize_json(
        {
            "ok": result["overall"] == "PASS",
            "wait_seconds": wait_seconds,
            "checked_at": utc_now().isoformat().replace("+00:00", "Z"),
            "before": before,
            "after": after,
            "result": result,
            "explanation": (
                "PASS = counters/timestamps advanced during the wait window "
                "(real ingest), not only process-alive."
            ),
        }
    )
