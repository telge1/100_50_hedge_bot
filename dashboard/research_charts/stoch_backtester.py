"""Load Stoch-Signale as TRP long/short position drawings (Entry/TP/SL)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from stoch_signal_source import (
    SOURCE_RESEARCH_1M_TIMING,
    frozen_upstream_path,
    get_dashboard_signal_source,
    get_default_research_display_variant,
    research_upstream_path,
)

from .collector_control import COLLECTOR_API_BASE
from .trp_import import load_trp

BACKTESTER_SOURCE = "stoch_backtester"
DEFAULT_OPEN_WIDTH = timedelta(hours=4)


def _parse_dt(value: Any) -> datetime | None:
    trp = load_trp()
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return trp["ensure_utc"](value)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return trp["ensure_utc"](datetime.fromisoformat(text))
    except ValueError:
        return None


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out <= 0:
        return None
    return out


def signal_to_position_spec(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one Stoch-Signale row to a position drawing spec."""
    trp = load_trp()
    symbol = str(row.get("symbol") or "").strip().upper()
    direction = str(row.get("trade_direction") or row.get("direction") or "").upper()
    if direction in ("BUY", "L"):
        direction = "LONG"
    if direction in ("SELL", "S"):
        direction = "SHORT"
    if not symbol or direction not in ("LONG", "SHORT"):
        return None
    entry = _f(row.get("entry_price")) or _f(row.get("expected_open_price")) or _f(row.get("signal_price"))
    tp = (
        _f(row.get("tp_price"))
        or _f(row.get("expected_tp"))
        or _f(row.get("tp1_price"))
    )
    sl = _f(row.get("sl_price")) or _f(row.get("expected_sl")) or _f(row.get("pool_sl_price"))
    if entry is None or tp is None or sl is None:
        return None
    start = (
        _parse_dt(row.get("entry_time"))
        or _parse_dt(row.get("candle_close_time"))
        or _parse_dt(row.get("signal_time"))
        or _parse_dt(row.get("expected_open_time"))
        or _parse_dt(row.get("generated_at"))
    )
    if start is None:
        return None
    end = _parse_dt(row.get("exit_time"))
    dur = row.get("duration_seconds")
    if end is None and dur is not None:
        try:
            end = start + timedelta(seconds=int(dur))
        except (TypeError, ValueError):
            end = None
    if end is None:
        end = trp["ensure_utc"](datetime.now(timezone.utc))
        if end <= start:
            end = start + DEFAULT_OPEN_WIDTH
    if end <= start:
        end = start + timedelta(minutes=15)
    tf = str(row.get("timeframe") or "5m").strip() or "5m"
    sid = str(row.get("signal_id") or row.get("id") or "").strip()
    drawing_type = "long_position" if direction == "LONG" else "short_position"
    return {
        "drawing_id": f"stoch-{sid}" if sid else None,
        "drawing_type": drawing_type,
        "symbol": symbol,
        "timeframe": tf,
        "start": start,
        "end": end,
        "entry": entry,
        "stop": sl,
        "target": tp,
        "signal_id": sid,
        "direction": direction,
    }


def fetch_stoch_signal_rows(
    *,
    symbol: str,
    hours: int = 48,
    limit: int = 500,
    strategy_version: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Same feed as /stoch-signale. Pool V1 uses the research artifact, not collector :8787."""
    sv = str(strategy_version or "").strip()
    if sv == "POOL_ORDER_PLAN_V1":
        from pool_order_plan_v1.config import enable_pool_order_plan_v1
        from pool_order_plan_v1.research_feed import research_signals_response

        if not enable_pool_order_plan_v1():
            return [], "Pool-V1 ist deaktiviert"
        payload = research_signals_response(symbol=symbol)
        if not payload.get("feed_ready"):
            return [], str(payload.get("message") or "Pool-V1-Artefakt nicht verfügbar")
        raw = payload.get("signals") or payload.get("items") or []
        return [r for r in raw if isinstance(r, dict)], None

    src = get_dashboard_signal_source()
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=max(1, int(hours)))
    params: dict[str, Any] = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "time_field": "candle_close_time",
        "limit": str(limit),
        "offset": "0",
        "symbol": symbol.upper(),
        "tier_a": "true",
    }
    if src == SOURCE_RESEARCH_1M_TIMING:
        path = research_upstream_path()
        params["timing_variant"] = get_default_research_display_variant()
    else:
        path = frozen_upstream_path()
        params["strategy_version"] = "wave_fade_no_be50_v1"
    url = f"{COLLECTOR_API_BASE}{path}"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url, params=params)
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    if not isinstance(payload, dict) or resp.status_code >= 400:
        err = None
        if isinstance(payload, dict):
            err = str(payload.get("error") or payload.get("detail") or f"http_{resp.status_code}")
        return [], err or "signal_feed_unavailable"
    raw = payload.get("signals") or payload.get("items") or []
    return [r for r in raw if isinstance(r, dict)], None
