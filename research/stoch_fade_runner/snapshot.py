"""Read-only production snapshots. SELECT only. Scoped to the run symbol."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import DEFAULT_SG_ROOT, REPO_ROOT, sg_root
from .jsonio import write_json_atomic
from .query import ReadOnlyQueryClient

CANDLE_FILTERS = {
    "database": "signal_generator",
    "table": "candles_1m",
    "final": True,
    "exchange": "bybit",
    "interval": "1m",
    "is_closed": 1,
    "window": "[start, end)",
}


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if hasattr(ts, "isoformat"):
        t = ts
        if getattr(t, "tzinfo", None) is None:
            t = t.replace(tzinfo=timezone.utc)
        else:
            t = t.astimezone(timezone.utc)
        return t.isoformat().replace("+00:00", "Z")
    return str(ts)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _one(client: ReadOnlyQueryClient, sql: str, parameters: dict | None = None) -> Any:
    result = client.query(sql, parameters=parameters)
    rows = result.result_rows
    return rows[0] if rows else None


def _try_one(client: ReadOnlyQueryClient, sql: str, parameters: dict | None = None) -> tuple[Any, str | None]:
    try:
        return _one(client, sql, parameters=parameters), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _try_query(client: ReadOnlyQueryClient, sql: str, parameters: dict | None = None) -> tuple[Any, str | None]:
    try:
        return client.query(sql, parameters=parameters), None
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def _outcomes_payload(outcomes: Any, *, scope_symbol: str) -> dict[str, Any]:
    if isinstance(outcomes, tuple) and outcomes and outcomes[0] == "error":
        return {
            "count": None,
            "uniq_signal_id": None,
            "error": outcomes[1],
            "scope": "error",
            "scope_symbol": scope_symbol,
        }
    if isinstance(outcomes, tuple) and outcomes and outcomes[0] == "global_only":
        return {
            "count": outcomes[1],
            "uniq_signal_id": outcomes[2],
            "error": outcomes[3],
            "scope": "global_only",
            "scope_symbol": scope_symbol,
        }
    if outcomes is None:
        return {
            "count": 0,
            "uniq_signal_id": 0,
            "error": None,
            "scope": "symbol_via_signal_id_join",
            "scope_symbol": scope_symbol,
        }
    return {
        "count": int(outcomes[0]),
        "uniq_signal_id": int(outcomes[1]),
        "error": None,
        "scope": "symbol_via_signal_id_join",
        "scope_symbol": scope_symbol,
    }


def capture_snapshot(
    client: ReadOnlyQueryClient,
    *,
    label: str,
    symbol: str,
    start: datetime,
    end: datetime,
) -> dict[str, Any]:
    if not symbol:
        raise ValueError("snapshot requires explicit run symbol")
    db = client.database
    params = {"symbol": symbol, "start": start, "end": end}
    candle = _one(
        client,
        f"""
        SELECT
            min(open_time),
            max(open_time),
            count(),
            uniqExact(open_time)
        FROM {db}.candles_1m FINAL
        WHERE exchange = 'bybit'
          AND symbol = {{symbol:String}}
          AND interval = '1m'
          AND is_closed = 1
        """,
        {"symbol": symbol},
    )
    window_gaps = _one(
        client,
        f"""
        SELECT
            min(open_time),
            max(open_time),
            count(),
            uniqExact(open_time),
            count() - uniqExact(open_time) AS duplicate_count,
            dateDiff('minute', min(open_time), max(open_time)) + 1 - uniqExact(open_time) AS internal_1m_gaps
        FROM {db}.candles_1m FINAL
        WHERE exchange = 'bybit'
          AND symbol = {{symbol:String}}
          AND interval = '1m'
          AND is_closed = 1
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        """,
        params,
    )
    by_exchange = client.query(
        f"""
        SELECT exchange, count(), uniqExact(open_time)
        FROM {db}.candles_1m FINAL
        WHERE symbol = {{symbol:String}}
          AND interval = '1m'
          AND is_closed = 1
          AND open_time >= {{start:DateTime64(3, 'UTC')}}
          AND open_time < {{end:DateTime64(3, 'UTC')}}
        GROUP BY exchange
        ORDER BY exchange
        """,
        params,
    )
    sig, sig_err = _try_one(
        client,
        f"""
        SELECT
            count(),
            uniqExact(signal_id),
            min(generated_at),
            max(generated_at)
        FROM {db}.signals FINAL
        WHERE symbol = {{symbol:String}}
        """,
        {"symbol": symbol},
    )
    watermarks, wm_err = _try_query(
        client,
        f"""
        SELECT symbol, timeframe, strategy_version, last_processed_candle_open_time
        FROM {db}.signal_processing_state FINAL
        WHERE symbol = {{symbol:String}}
        ORDER BY timeframe, strategy_version
        """,
        {"symbol": symbol},
    )
    outcomes, outcomes_err = _try_one(
        client,
        f"""
        SELECT count(), uniqExact(o.signal_id)
        FROM {db}.signal_outcomes AS o FINAL
        INNER JOIN {db}.signals AS s FINAL ON o.signal_id = s.signal_id
        WHERE s.symbol = {{symbol:String}}
        """,
        {"symbol": symbol},
    )
    if outcomes_err:
        fallback, fallback_err = _try_one(
            client, f"SELECT count(), uniqExact(signal_id) FROM {db}.signal_outcomes FINAL"
        )
        if fallback_err:
            outcomes = ("error", fallback_err)
        else:
            outcomes = (
                "global_only",
                int(fallback[0]) if fallback else 0,
                int(fallback[1]) if fallback else 0,
                outcomes_err,
            )

    global_counts = {}
    for table in ("candles_1m", "signals", "signal_processing_state"):
        row, gerr = _try_one(client, f"SELECT count() FROM {db}.{table}")
        global_counts[table] = int(row[0]) if row and not gerr else None

    sg = sg_root()
    payload = {
        "label": label,
        "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope_symbol": symbol,
        "scope_start": _iso(start),
        "scope_end_exclusive": _iso(end),
        "candle_filters": dict(CANDLE_FILTERS) | {"symbol": symbol},
        "symbol": symbol,
        "candles": {
            "symbol": symbol,
            "min_open_time": _iso(candle[0]) if candle else None,
            "max_open_time": _iso(candle[1]) if candle else None,
            "count": int(candle[2]) if candle else 0,
            "uniq_open_time": int(candle[3]) if candle else 0,
        },
        "window_candles": {
            **CANDLE_FILTERS,
            "symbol": symbol,
            "start": _iso(start),
            "end": _iso(end),
            "min_open_time": _iso(window_gaps[0]) if window_gaps else None,
            "max_open_time": _iso(window_gaps[1]) if window_gaps else None,
            "count": int(window_gaps[2]) if window_gaps else 0,
            "uniq_open_time": int(window_gaps[3]) if window_gaps else 0,
            "duplicate_count": int(window_gaps[4]) if window_gaps else 0,
            "internal_1m_gaps": int(window_gaps[5]) if window_gaps else 0,
            "count_by_exchange": [
                {"exchange": r[0], "count": int(r[1]), "uniq_open_time": int(r[2])}
                for r in by_exchange.result_rows
            ],
        },
        "signals": {
            "symbol": symbol,
            "count": int(sig[0]) if sig else 0,
            "uniq_signal_id": int(sig[1]) if sig else 0,
            "min_generated_at": _iso(sig[2]) if sig else None,
            "max_generated_at": _iso(sig[3]) if sig else None,
            "unavailable": sig_err,
        },
        "watermarks": [
            {
                "symbol": r[0],
                "timeframe": r[1],
                "strategy_version": r[2],
                "last_processed_candle_open_time": _iso(r[3]),
            }
            for r in (watermarks.result_rows if watermarks is not None else [])
        ],
        "outcomes": _outcomes_payload(outcomes, scope_symbol=symbol),
        "global_control_counts": global_counts,
        "global_table_counts": global_counts,
        "global_counts_are_not_coin_writes": True,
        "files": {
            "live_universe.json": _sha256_file(sg / "config" / "live_universe.json"),
            "pool_research_registry.json": _sha256_file(
                REPO_ROOT / "dashboard" / "pool_order_plan_v1" / "research_registry.json"
            ),
            "ema_research_registry.json": _sha256_file(
                REPO_ROOT / "dashboard" / "ema_pool_trend_flip_v1" / "research_registry.json"
            ),
            "stoch_signale_default": "wave_fade_no_be50_v1",
        },
        "sg_root": str(DEFAULT_SG_ROOT),
        "production_tables_optional": True,
        "production_signals_unavailable": sig_err,
        "production_watermarks_unavailable": wm_err,
    }
    return payload


def coin_scope_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("scope_symbol", "scope_start", "scope_end_exclusive", "candles", "window_candles", "signals", "watermarks", "outcomes")
    return all(before.get(k) == after.get(k) for k in keys)


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    write_json_atomic(path, payload)
