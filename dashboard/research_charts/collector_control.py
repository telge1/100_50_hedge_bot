"""Orchestrate the existing live collector. No new process, WS, or recovery."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx

from .forming_from_ch import forming_bar_from_clickhouse
from .live_universe import (
    HISTORY_AVAILABLE_AND_LIVE_CONFIGURED,
    HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED,
    classify_live_capability,
    is_btc_rejected,
    is_live_configured,
    load_live_universe_symbols,
)

COLLECTOR_API_BASE = os.environ.get("STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787").rstrip("/")
POLL_INTERVAL_S = 5
POLL_INTERVAL_MS = POLL_INTERVAL_S * 1000

# Last-good forming tip: collector often returns null briefly (~minute roll / WS gap).
_FORMING_HOLD_S = 15.0
_forming_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_forming_lock = threading.Lock()

ALREADY_ACTIVE_STATES = frozenset(
    {
        "STARTING",
        "RECOVERING",
        "CONNECTING",
        "SUBSCRIBING",
        "LIVE",
        "RECONNECTING",
        "DEGRADED",
    }
)

UI_STATUS_HISTORICAL = "HISTORICAL"
UI_STATUS_RECOVERING = "RECOVERING"
UI_STATUS_LIVE = "LIVE"
UI_STATUS_STALE = "STALE"
UI_STATUS_ERROR = "ERROR"
UI_STATUS_UNAVAILABLE = "UNAVAILABLE"
UI_STATUS_LIVE_NOT_AVAILABLE = "LIVE_NOT_AVAILABLE"


def _base() -> str:
    return os.environ.get("STOCH_COLLECTOR_API_BASE", COLLECTOR_API_BASE).rstrip("/")


def unavailable_status(*, detail: str = "collector_api_unreachable") -> dict[str, Any]:
    return {
        "collector_available": False,
        "collector_state": "UNAVAILABLE",
        "desired_state": None,
        "websocket_connected": False,
        "configured_symbols": [],
        "subscribed_symbols": [],
        "live_symbols": [],
        "stale_symbols": [],
        "recovering_symbols": [],
        "symbols": [],
        "error": detail,
    }


def _valid_collector_forming(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    try:
        ft = int(raw["time"])
        px = float(raw["close"])
    except (TypeError, ValueError, KeyError):
        return None
    if ft <= 0 or px <= 0:
        return None
    return dict(raw)


def _stamp_forming_metadata(bar: dict[str, Any], *, source: str) -> dict[str, Any]:
    out = dict(bar)
    out["forming_source"] = source
    out.setdefault("forming", True)
    try:
        ft = int(out["time"])
        out["forming_lag_seconds"] = max(0, int(time.time()) - ft)
    except (TypeError, ValueError, KeyError):
        pass
    return out


def _fetch_collector_forming(
    sym: str, *, timeout: float
) -> tuple[dict[str, Any] | None, str]:
    """Return (bar, reason). reason is ok | null_forming | http_error | unreachable."""
    url = f"{_base()}/api/collector/forming"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params={"symbol": sym})
        if resp.status_code >= 400:
            return None, "http_error"
        try:
            payload = resp.json()
        except Exception:
            return None, "http_error"
        if not isinstance(payload, dict):
            return None, "null_forming"
        bar = _valid_collector_forming(payload.get("forming"))
        if bar is None:
            return None, "null_forming"
        return bar, "ok"
    except (httpx.ConnectError, httpx.TimeoutException):
        return None, "unreachable"
    except Exception:
        return None, "unreachable"


def fetch_forming_candle(symbol: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
    """Collector forming first; ClickHouse partial-bucket fallback for Research UI.

    The collector clears forming around minute rolls / reconnect gaps (~10% of polls).
    Without a short hold, Research Charts look frozen even though the next tick is fine.
    When the control API is down, synthesize from closed 1m in ClickHouse.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None

    bar, reason = _fetch_collector_forming(sym, timeout=timeout)
    now = time.monotonic()
    if bar is not None:
        stamped = _stamp_forming_metadata(bar, source="collector")
        with _forming_lock:
            _forming_cache[sym] = (now, dict(stamped))
        return stamped

    if reason == "null_forming":
        with _forming_lock:
            cached = _forming_cache.get(sym)
            if cached and (now - float(cached[0])) <= _FORMING_HOLD_S:
                out = _stamp_forming_metadata(dict(cached[1]), source="collector")
                out["_stale_hold"] = True
                return out

    try:
        ch_bar = forming_bar_from_clickhouse(sym)
    except Exception:
        ch_bar = None
    if ch_bar is not None:
        return ch_bar
    return None


def fetch_collector_status(*, timeout: float = 5.0) -> dict[str, Any]:
    """GET existing control API. Never starts a process."""
    url = f"{_base()}/api/collector/status"
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url)
        try:
            payload = resp.json()
        except Exception:
            return unavailable_status(detail="invalid_json_from_collector")
        if not isinstance(payload, dict):
            return unavailable_status(detail="invalid_status_payload")
        if resp.status_code >= 400:
            payload = dict(payload)
            payload["collector_available"] = False
            payload.setdefault("collector_state", "UNAVAILABLE")
            payload.setdefault("error", payload.get("detail") or f"http_{resp.status_code}")
            return payload
        payload = dict(payload)
        payload["collector_available"] = True
        payload.setdefault("collector_state", payload.get("state") or "UNKNOWN")
        return payload
    except httpx.ConnectError:
        return unavailable_status(detail="collector_api_unreachable")
    except httpx.TimeoutException:
        return unavailable_status(detail="collector_api_timeout")
    except Exception as exc:  # noqa: BLE001
        return unavailable_status(detail=str(exc))


def set_desired_state(desired: str, *, timeout: float = 5.0) -> dict[str, Any]:
    value = str(desired).upper()
    if value not in ("RUNNING", "STOPPED"):
        raise ValueError("desired_state must be RUNNING or STOPPED")
    url = f"{_base()}/api/collector/desired_state"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json={"desired_state": value})
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": "invalid_json_from_collector"}
    if not isinstance(payload, dict):
        payload = {"error": "invalid_desired_payload"}
    payload["http_status"] = resp.status_code
    return payload


def ensure_symbol_on_collector(symbol: str, *, timeout: float = 8.0) -> dict[str, Any]:
    """Ask the existing collector to recover+live only this symbol."""
    url = f"{_base()}/api/collector/ensure_symbol"
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url, json={"symbol": str(symbol).strip().upper()})
    try:
        payload = resp.json()
    except Exception:
        payload = {"error": "invalid_json_from_collector"}
    if not isinstance(payload, dict):
        payload = {"error": "invalid_ensure_payload"}
    payload["http_status"] = resp.status_code
    return payload


def _symbol_row(status: dict[str, Any], symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    for row in status.get("symbols") or []:
        if str(row.get("symbol") or "").upper() == sym:
            return dict(row)
    return {}


def map_research_ui_status(
    *,
    history_available: bool,
    live_configured: bool,
    collector_available: bool,
    collector_state: str | None,
    symbol_state: str | None,
    btc_rejected: bool,
) -> str:
    if btc_rejected:
        return UI_STATUS_LIVE_NOT_AVAILABLE
    if history_available and not live_configured:
        return UI_STATUS_HISTORICAL
    if not collector_available:
        return UI_STATUS_UNAVAILABLE if history_available else UI_STATUS_ERROR
    st = str(symbol_state or "").upper()
    cs = str(collector_state or "").upper()
    if st == "STALE" or cs == "DEGRADED":
        return UI_STATUS_STALE if st == "STALE" else UI_STATUS_ERROR
    if st == "ERROR" or cs == "ERROR":
        return UI_STATUS_ERROR
    if st == "RECOVERING" or cs == "RECOVERING":
        return UI_STATUS_RECOVERING
    if st == "LIVE" or cs == "LIVE":
        return UI_STATUS_LIVE
    if cs in {"STARTING", "CONNECTING", "SUBSCRIBING", "RECONNECTING"}:
        return UI_STATUS_RECOVERING
    if history_available:
        return UI_STATUS_HISTORICAL
    return UI_STATUS_ERROR


def ensure_live_collector(symbol: str) -> dict[str, Any]:
    """Start/switch the existing collector to this symbol (demand singleton).

    Never starts a second process. BTCUSDT is rejected. Other symbols do not
    need to be in live_universe.json — demand replaces the active set.
    """
    sym = str(symbol or "").strip().upper()
    universe = load_live_universe_symbols()
    btc = is_btc_rejected(sym)
    live_configured = (not btc) and bool(sym)
    status = fetch_collector_status()
    available = bool(status.get("collector_available"))
    collector_state = str(status.get("collector_state") or "UNAVAILABLE").upper()
    desired = str(status.get("desired_state") or "").upper() or None
    row = _symbol_row(status, sym)
    symbol_state = str(row.get("state") or "") or None

    result: dict[str, Any] = {
        "symbol": sym,
        "live_configured": live_configured,
        "live_universe": universe,
        "btc_rejected": btc,
        "collector_available": available,
        "collector_state": collector_state,
        "desired_state": desired,
        "symbol_state": symbol_state,
        "action": "none",
        "ensured": False,
        "reason": None,
    }

    if not sym:
        result["reason"] = "invalid_symbol"
        return result
    if btc:
        result["reason"] = "btc_rejected"
        return result
    if not available:
        result["reason"] = "collector_unavailable"
        return result

    live_syms = [
        str(s).upper()
        for s in (status.get("configured_symbols") or status.get("live_symbols") or [])
    ]
    if (
        collector_state in ALREADY_ACTIVE_STATES
        and desired == "RUNNING"
        and live_syms == [sym]
    ):
        result["ensured"] = True
        result["action"] = "already_running"
        result["reason"] = "already_on_symbol"
        return result

    try:
        posted = ensure_symbol_on_collector(sym)
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"ensure_symbol_failed:{exc}"
        return result
    if int(posted.get("http_status") or 500) >= 400:
        result["reason"] = posted.get("error") or "ensure_symbol_rejected"
        result["post_response"] = posted
        return result
    result["ensured"] = True
    result["action"] = "ensure_symbol"
    result["reason"] = "demand_singleton"
    result["post_response"] = posted
    result["desired_state"] = "RUNNING"
    return result


def live_status_for_symbol(
    symbol: str,
    *,
    history_available: bool,
    last_closed_time: int | None = None,
    ensure: bool = False,
) -> dict[str, Any]:
    sym = str(symbol or "").strip().upper()
    universe = load_live_universe_symbols()
    btc = is_btc_rejected(sym)
    live_configured = (not btc) and bool(sym)
    capability = classify_live_capability(
        history_available=history_available, live_configured=live_configured
    )
    ensured: dict[str, Any] | None = None
    if ensure and live_configured and not btc:
        ensured = ensure_live_collector(sym)
        status = fetch_collector_status()
    else:
        status = fetch_collector_status()

    available = bool(status.get("collector_available"))
    collector_state = str(status.get("collector_state") or "UNAVAILABLE").upper()
    row = _symbol_row(status, sym)
    symbol_state = str(row.get("state") or "") or None
    last_from_collector = row.get("last_closed_candle_at") or row.get("last_persisted_open_time")
    ui = map_research_ui_status(
        history_available=history_available,
        live_configured=live_configured,
        collector_available=available,
        collector_state=collector_state,
        symbol_state=symbol_state,
        btc_rejected=btc,
    )
    if capability == HISTORY_AVAILABLE_BUT_NOT_LIVE_CONFIGURED and not btc:
        ui = UI_STATUS_HISTORICAL
    if capability == HISTORY_AVAILABLE_AND_LIVE_CONFIGURED and not available:
        ui = UI_STATUS_UNAVAILABLE

    return {
        "symbol": sym,
        "history_available": bool(history_available),
        "live_configured": live_configured,
        "live_capability": capability,
        "btc_rejected": btc,
        "collector_available": available,
        "collector_state": collector_state,
        "desired_state": status.get("desired_state"),
        "symbol_state": symbol_state,
        "last_closed_time": last_closed_time,
        "last_closed_candle_at": last_from_collector,
        "research_ui_status": ui,
        "websocket_connected": bool(status.get("websocket_connected")),
        "ensure": ensured,
        "poll_interval_ms": POLL_INTERVAL_MS,
    }
