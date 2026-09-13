"""Causal Open Interest series for Research / Market Profile lower panes.

Source of truth:
  - Live: orderbook_analysis.open_interest_5s (WS 5s buckets)
  - History fallback: orderbook_analysis.open_interest_5m_history (REST)

Never infers 5s from 5m. Alignment is last sample at or before each candle's
causal as-of (bar close, clamped to now). Read-only ClickHouse.
"""

from __future__ import annotations

import logging
import time
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any, Iterable

import clickhouse_connect

from collector_health.ch_config import load_orderbook_ch_config

logger = logging.getLogger(__name__)

OI_5S_FQN = "orderbook_analysis.open_interest_5s"
OI_5M_FQN = "orderbook_analysis.open_interest_5m_history"
OI_COLOR = "#f0b90b"
LIVE_MAX_SECONDS = 3 * 24 * 3600
ASOF_LOOKBACK_SECONDS = 6 * 3600
QUERY_TIMEOUT_S = 8
QUERY_MEMORY_BYTES = 120_000_000
QUERY_THREADS = 2

_TF_SEC = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
}


def empty_payload(*, visible: bool = False, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": "open_interest",
        "title": "Open Interest",
        "visible": bool(visible),
        "auto_scale": True,
        "series": [],
        "levels": [],
        "source": None,
    }
    if error:
        out["error"] = error
    return out


def _qsettings() -> dict[str, int]:
    return {
        "max_execution_time": QUERY_TIMEOUT_S,
        "max_memory_usage": QUERY_MEMORY_BYTES,
        "max_threads": QUERY_THREADS,
    }


def _client():
    cfg = load_orderbook_ch_config()
    return clickhouse_connect.get_client(**cfg.connect_kwargs())


def _utc_unix(ts: Any) -> int | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return int(ts.astimezone(timezone.utc).timestamp())
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


def candle_unix(candle: Any) -> int | None:
    if isinstance(candle, dict):
        return _utc_unix(candle.get("time") if candle.get("time") is not None else candle.get("timestamp"))
    ts = getattr(candle, "timestamp", None)
    if ts is not None:
        return _utc_unix(ts)
    return _utc_unix(getattr(candle, "time", None))


def plot_and_asof_times(
    candles: Iterable[Any],
    *,
    timeframe: str = "5m",
    now: int | None = None,
) -> tuple[list[int], list[int]]:
    """Plot at bar open (chart time); as-of at bar close clamped to now."""
    step = int(_TF_SEC.get(str(timeframe or "5m"), 60))
    now_ts = int(now if now is not None else time.time())
    plot: list[int] = []
    asof: list[int] = []
    for candle in candles:
        t = candle_unix(candle)
        if t is None:
            continue
        plot.append(int(t))
        asof.append(min(now_ts, int(t) + step))
    return plot, asof


def _last_at_or_before(samples: list[tuple[int, float]], t: int) -> tuple[int, float] | None:
    if not samples:
        return None
    times = [int(row[0]) for row in samples]
    i = bisect_right(times, int(t)) - 1
    if i < 0:
        return None
    return int(samples[i][0]), float(samples[i][1])


def align_asof(asof_times: list[int], samples: list[tuple[int, float]]) -> list[float | None]:
    """Last sample at or before each as-of time. samples must be sorted by ts."""
    return [
        None if row is None else row[1]
        for row in (_last_at_or_before(samples, int(t)) for t in asof_times)
    ]


def merge_live_over_history(
    plot_times: list[int],
    asof_times: list[int],
    live: list[tuple[int, float]],
    hist: list[tuple[int, float]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Use the newest sample at or before as-of; prefer 5s when timestamps tie."""
    used_live = False
    used_hist = False
    data: list[dict[str, Any]] = []
    for i, plot_t in enumerate(plot_times):
        t = int(asof_times[i])
        live_row = _last_at_or_before(live, t)
        hist_row = _last_at_or_before(hist, t)
        chosen = None
        from_live = False
        if live_row and hist_row:
            if live_row[0] >= hist_row[0]:
                chosen = live_row[1]
                from_live = True
            else:
                chosen = hist_row[1]
        elif live_row:
            chosen = live_row[1]
            from_live = True
        elif hist_row:
            chosen = hist_row[1]
        if chosen is None:
            continue
        if from_live:
            used_live = True
        else:
            used_hist = True
        data.append({"time": int(plot_t), "value": float(chosen)})
    if used_live and used_hist:
        source = "mixed"
    elif used_live:
        source = "open_interest_5s"
    elif used_hist:
        source = "open_interest_5m_history"
    else:
        source = None
    return data, source


def _query_samples(table: str, symbol: str, start: int, end: int) -> list[tuple[int, float]]:
    if end < start:
        return []
    sql = (
        f"SELECT toUnixTimestamp(bucket_time) AS ts, toFloat64(open_interest) AS oi "
        f"FROM {table} "
        f"WHERE symbol = {{symbol:String}} "
        f"  AND bucket_time >= toDateTime({{start:UInt32}}) "
        f"  AND bucket_time <= toDateTime({{end:UInt32}}) "
        f"ORDER BY bucket_time"
    )
    client = _client()
    try:
        result = client.query(
            sql,
            parameters={"symbol": symbol, "start": int(start), "end": int(end)},
            settings=_qsettings(),
        )
    finally:
        try:
            client.close()
        except Exception:
            pass
    out: list[tuple[int, float]] = []
    for row in result.result_rows:
        ts = _utc_unix(row[0])
        if ts is None or row[1] is None:
            continue
        try:
            val = float(row[1])
        except (TypeError, ValueError):
            continue
        out.append((ts, val))
    return out


def candles_from_times(times: Iterable[Any]) -> list[dict[str, int]]:
    """Strictly increasing unix plot times as candle-shaped dicts."""
    if not isinstance(times, (list, tuple)):
        return []
    out: list[dict[str, int]] = []
    prev: int | None = None
    count = 0
    for raw in times:
        if count >= 20000:
            break
        t = candle_unix(raw) if isinstance(raw, dict) else _utc_unix(raw)
        count += 1
        if t is None:
            continue
        if prev is not None and int(t) <= prev:
            continue
        prev = int(t)
        out.append({"time": prev})
    return out


def fetch_oi_samples(
    symbol: str,
    start: int,
    end: int,
    *,
    now: int | None = None,
) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """Return (live_5s, hist_5m). 5s covers the last LIVE_MAX_SECONDS of the view."""
    now_ts = int(now if now is not None else time.time())
    end_ts = min(int(end), now_ts)
    hist_start = max(0, int(start) - ASOF_LOOKBACK_SECONDS)
    live_start = max(hist_start, end_ts - LIVE_MAX_SECONDS)
    live: list[tuple[int, float]] = []
    hist: list[tuple[int, float]] = []
    if live_start <= end_ts:
        live = _query_samples(OI_5S_FQN, symbol, live_start, end_ts)
    hist = _query_samples(OI_5M_FQN, symbol, hist_start, end_ts)
    return live, hist


def payload_from_series(
    data: list[dict[str, Any]],
    *,
    source: str | None,
    visible: bool = True,
) -> dict[str, Any]:
    return {
        "id": "open_interest",
        "title": "Open Interest",
        "visible": bool(visible),
        "auto_scale": True,
        "series": [
            {
                "id": "oi",
                "title": "OI",
                "color": OI_COLOR,
                "auto_scale": True,
                "data": data,
            }
        ]
        if data
        else [],
        "levels": [],
        "source": source,
    }


def load_open_interest_payload(
    symbol: str,
    candles: Iterable[Any],
    *,
    enabled: bool,
    timeframe: str = "5m",
    now: int | None = None,
) -> dict[str, Any]:
    if not enabled:
        return empty_payload(visible=False)
    sym = str(symbol or "").strip().upper()
    plot_times, asof = plot_and_asof_times(candles, timeframe=timeframe, now=now)
    if not sym or not asof:
        return empty_payload(visible=True)
    try:
        live, hist = fetch_oi_samples(sym, plot_times[0], asof[-1], now=now)
        data, source = merge_live_over_history(plot_times, asof, live, hist)
        return payload_from_series(data, source=source, visible=True)
    except Exception as exc:
        logger.warning("open interest load failed for %s: %s", sym, exc)
        return empty_payload(visible=True, error=str(exc))
