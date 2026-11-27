"""Central price-alert monitor — publicTrade fanout (primary) + forming fallback."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

from price_alert_store import (
    append_fired_event,
    condition_met,
    crossed_level,
    load_state,
    save_state,
)
from utils.notifications import play_alert_sound, send_ntfy_alert

logger = logging.getLogger(__name__)

_COLLECTOR_BASE = os.environ.get("STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787").rstrip("/")
_XAU_COLLECTOR_BASE = os.environ.get("XAU_COLLECTOR_API_BASE", "http://127.0.0.1:8788").rstrip("/")
_QNT_COLLECTOR_BASE = os.environ.get("QNT_COLLECTOR_API_BASE", "http://127.0.0.1:8789").rstrip("/")

_FANOUT_POLL_S = 0.35
_FORMING_POLL_S = 0.9
_FANOUT_PROBE_S = 5.0
_FANOUT_STALE_S = 2.5
_FORMING_STALE_S = 8.0
_SUBSCRIBER_ID = "dashboard_price_alert_monitor_v1"

_STATUS_LIVE_PUBLIC_TRADE = "LIVE_PUBLIC_TRADE"
_STATUS_LIVE_FORMING = "LIVE_FORMING_FALLBACK"
_STATUS_STALE = "STALE"
_STATUS_UNAVAILABLE = "UNAVAILABLE"

_FORMING_ONLY_SYMBOLS = frozenset({"QNTUSDT", "XAUUSDT"})

_cross_reference: dict[str, float] = {}
_last_price_by_symbol: dict[str, float] = {}
_symbol_health: dict[str, dict[str, Any]] = {}
_initialized_alert_keys: set[str] = set()
_firing_keys: set[str] = set()

_fanout_available: bool | None = None
_fanout_last_probe_at: float = 0.0
_fanout_last_event_at: dict[str, float] = {}
_fanout_cursor: int | None = None
_fanout_coverage_valid: bool | None = None
_fanout_last_error: str | None = None

_monitor_lock = threading.Lock()
_subscriber_ready = False
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _collector_base_for_symbol(symbol: str) -> str:
    sym = str(symbol).upper()
    if sym == "QNTUSDT":
        return _QNT_COLLECTOR_BASE
    if sym == "XAUUSDT":
        return _XAU_COLLECTOR_BASE
    return _COLLECTOR_BASE


def _uses_fanout_primary(symbol: str) -> bool:
    return str(symbol).upper() not in _FORMING_ONLY_SYMBOLS


def _sync_alert_reference_seeds() -> None:
    """Arm crossing baseline only — does not block live updates."""
    state = load_state()
    _initialized_alert_keys.intersection_update(set(state.keys()))
    for key, entry in state.items():
        if not isinstance(entry, dict) or entry.get("engine") != "monitor_v1":
            continue
        if key in _initialized_alert_keys:
            continue
        sym = str(entry.get("symbol") or "").upper()
        ref = entry.get("reference_price")
        if sym and isinstance(ref, (int, float)) and float(ref) > 0:
            if sym not in _cross_reference:
                _cross_reference[sym] = float(ref)
        _initialized_alert_keys.add(key)


def _touch_symbol_health(
    sym: str,
    *,
    price: float,
    source: str,
    status: str,
    extra: dict[str, Any] | None = None,
) -> None:
    now = time.time()
    row = _symbol_health.get(sym) or {}
    row.update(
        {
            "symbol": sym,
            "source": source,
            "status": status,
            "last_price": float(price),
            "last_update_unix": now,
            "age_seconds": 0.0,
            "alert_count": _count_alerts_for_symbol(sym),
        }
    )
    if extra:
        row.update(extra)
    _symbol_health[sym] = row


def _count_alerts_for_symbol(sym: str) -> int:
    state = load_state()
    n = 0
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("engine") != "monitor_v1":
            continue
        if str(entry.get("symbol") or "").upper() == sym:
            n += 1
    return n


def _refresh_health_ages(symbols: set[str]) -> None:
    now = time.time()
    for sym in symbols:
        row = _symbol_health.get(sym)
        if not row:
            _symbol_health[sym] = {
                "symbol": sym,
                "status": _STATUS_UNAVAILABLE,
                "source": "none",
                "last_price": None,
                "last_update_unix": None,
                "age_seconds": None,
                "alert_count": _count_alerts_for_symbol(sym),
            }
            continue
        ts = row.get("last_update_unix")
        if ts:
            age = max(0.0, now - float(ts))
            row["age_seconds"] = round(age, 3)
            row["alert_count"] = _count_alerts_for_symbol(sym)
            if row.get("status") == _STATUS_LIVE_PUBLIC_TRADE and age > _FANOUT_STALE_S:
                row["status"] = _STATUS_STALE
            elif row.get("status") == _STATUS_LIVE_FORMING and age > _FORMING_STALE_S:
                row["status"] = _STATUS_STALE


def _probe_fanout(client: httpx.Client) -> None:
    global _fanout_available, _fanout_last_probe_at, _fanout_last_error
    _fanout_last_probe_at = time.monotonic()
    try:
        resp = client.post(
            f"{_COLLECTOR_BASE}/api/trade_fanout/trade_fanout_status",
            json={},
        )
        if resp.status_code == 404:
            _fanout_available = False
            _fanout_last_error = "fanout_endpoint_not_found"
            return
        data = resp.json()
        if data.get("ok"):
            _fanout_available = True
            _fanout_last_error = None
        else:
            _fanout_available = False
            _fanout_last_error = str(data.get("error") or "fanout_status_not_ok")
    except Exception as exc:
        _fanout_available = False
        _fanout_last_error = str(exc)[:200]


def _ensure_fanout_subscriber(client: httpx.Client) -> bool:
    global _subscriber_ready, _fanout_cursor
    if not _fanout_available:
        return False
    if _subscriber_ready:
        return True
    try:
        resp = client.post(
            f"{_COLLECTOR_BASE}/api/trade_fanout/create_trade_subscriber",
            json={
                "subscriber_id": _SUBSCRIBER_ID,
                "symbols": None,
                "max_queue": 4096,
            },
        )
        data = resp.json()
        if resp.status_code < 400 and data.get("ok"):
            _subscriber_ready = True
            _fanout_cursor = int(data.get("cursor") or 0)
            client.post(
                f"{_COLLECTOR_BASE}/api/trade_fanout/trade_subscriber_heartbeat",
                json={"subscriber_id": _SUBSCRIBER_ID},
            )
            return True
        _fanout_last_error = str(data.get("error") or f"http_{resp.status_code}")
    except Exception as exc:
        _fanout_last_error = str(exc)[:200]
    return False


def _poll_fanout(client: httpx.Client) -> list[dict[str, Any]]:
    global _fanout_cursor, _fanout_coverage_valid
    if not _ensure_fanout_subscriber(client):
        return []
    try:
        body: dict[str, Any] = {"subscriber_id": _SUBSCRIBER_ID, "limit": 256}
        if _fanout_cursor is not None:
            body["cursor"] = _fanout_cursor
        resp = client.post(f"{_COLLECTOR_BASE}/api/trade_fanout/poll_trade_events", json=body)
        data = resp.json()
        if not data.get("ok"):
            _fanout_last_error = str(data.get("error") or "poll_not_ok")
            if data.get("error") in ("cursor_ahead", "cursor_expired"):
                _fanout_coverage_valid = False
            return []
        _fanout_cursor = int(data.get("cursor") if data.get("cursor") is not None else _fanout_cursor or 0)
        _fanout_coverage_valid = bool(data.get("coverage_valid", True))
        cov = data.get("coverage") or {}
        if cov.get("overflow"):
            _fanout_last_error = "fanout_subscriber_overflow"
        events = data.get("events") or []
        return events if isinstance(events, list) else []
    except Exception as exc:
        _fanout_last_error = str(exc)[:200]
        return []


def _fetch_forming_close(symbol: str) -> dict[str, Any] | None:
    base = _collector_base_for_symbol(symbol)
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{base}/api/collector/forming", params={"symbol": symbol})
        if resp.status_code >= 400:
            return None
        payload = resp.json()
        forming = payload.get("forming") if isinstance(payload, dict) else None
        if not isinstance(forming, dict):
            return None
        close = float(forming.get("close"))
        if close <= 0:
            return None
        return {"close": close, "time": forming.get("time")}
    except Exception:
        return None


def _should_use_forming_fallback(sym: str, now: float) -> bool:
    if sym in _FORMING_ONLY_SYMBOLS:
        return True
    if not _fanout_available:
        return True
    last_ev = _fanout_last_event_at.get(sym)
    if last_ev is None or (now - last_ev) > _FANOUT_STALE_S:
        return True
    row = _symbol_health.get(sym) or {}
    if row.get("status") == _STATUS_STALE:
        return True
    return False


def _fire_alert(key: str, entry: dict[str, Any], fired_price: float, source: str) -> None:
    sym = str(entry.get("symbol") or "").upper()
    target = float(entry.get("target_price"))
    trigger = str(entry.get("trigger") or "above")
    with _monitor_lock:
        if key in _firing_keys:
            return
        state = load_state()
        if key not in state:
            return
        _firing_keys.add(key)
        state.pop(key, None)
        save_state(state)
    try:
        append_fired_event(
            alert_key=key,
            symbol=sym,
            target_price=target,
            trigger=trigger,
            fired_price=fired_price,
            source=source,
        )
        msg = f"{sym}: Preis {fired_price} {'≥' if trigger == 'above' else '≤'} {target}"
        tags = ["chart_with_upwards_trend"] if trigger == "above" else ["chart_with_downwards_trend"]
        send_ntfy_alert(message=msg, title="Price Alert", priority="high", tags=tags)
        play_alert_sound(repeats=2)
    except Exception as exc:
        logger.warning("price alert notify failed: %s", exc)
    finally:
        with _monitor_lock:
            _firing_keys.discard(key)
    logger.info("price alert fired %s @ %s (%s)", key, fired_price, source)
    _last_price_by_symbol[sym] = fired_price


def _check_symbol_prices(symbol: str, prices: list[float], *, source: str) -> None:
    sym = symbol.upper()
    state = load_state()
    if not state:
        return
    prev0 = _last_price_by_symbol.get(sym)
    if prev0 is None:
        prev0 = _cross_reference.get(sym)
    for key, entry in list(state.items()):
        if str(entry.get("symbol") or "").upper() != sym:
            continue
        if entry.get("engine") != "monitor_v1":
            continue
        target = float(entry.get("target_price"))
        trigger = str(entry.get("trigger") or "above")
        prev = prev0
        for px in prices:
            if not isinstance(px, (int, float)) or px <= 0:
                continue
            hit = (
                crossed_level(prev, px, target, trigger)
                if prev is not None
                else condition_met(px, target, trigger)
            )
            prev = px
            if hit:
                _fire_alert(key, entry, px, source)
                break
    if prices:
        _last_price_by_symbol[sym] = float(prices[-1])


def _active_monitor_symbols() -> set[str]:
    state = load_state()
    out: set[str] = set()
    for entry in state.values():
        if not isinstance(entry, dict):
            continue
        if entry.get("engine") != "monitor_v1":
            continue
        sym = str(entry.get("symbol") or "").upper()
        if sym:
            out.add(sym)
    return out


def _monitor_loop() -> None:
    last_forming: dict[str, float] = {}
    last_fanout: dict[str, float] = {"_probe": 0.0}
    client = httpx.Client(timeout=3.0)
    try:
        while not _stop_event.is_set():
            symbols = _active_monitor_symbols()
            if not symbols:
                time.sleep(0.5)
                continue
            _sync_alert_reference_seeds()
            now_mono = time.monotonic()
            now_wall = time.time()

            if now_mono - last_fanout.get("_probe", 0) >= _FANOUT_PROBE_S:
                last_fanout["_probe"] = now_mono
                _probe_fanout(client)
                if not _fanout_available:
                    _subscriber_ready = False

            if _fanout_available and now_mono - last_fanout.get("_poll", 0) >= _FANOUT_POLL_S:
                last_fanout["_poll"] = now_mono
                try:
                    client.post(
                        f"{_COLLECTOR_BASE}/api/trade_fanout/trade_subscriber_heartbeat",
                        json={"subscriber_id": _SUBSCRIBER_ID},
                    )
                except Exception:
                    pass
                for ev in _poll_fanout(client):
                    if not isinstance(ev, dict):
                        continue
                    sym = str(ev.get("symbol") or "").upper()
                    if sym not in symbols:
                        continue
                    try:
                        px = float(ev.get("price"))
                    except (TypeError, ValueError):
                        continue
                    _fanout_last_event_at[sym] = now_mono
                    _touch_symbol_health(
                        sym,
                        price=px,
                        source="public_trade_fanout",
                        status=_STATUS_LIVE_PUBLIC_TRADE,
                        extra={
                            "fanout_cursor": _fanout_cursor,
                            "coverage_valid": _fanout_coverage_valid,
                        },
                    )
                    _check_symbol_prices(sym, [px], source="public_trade_fanout")

            forming_due = now_mono - last_forming.get("_tick", 0) >= _FORMING_POLL_S
            if forming_due:
                last_forming["_tick"] = now_mono
                for sym in symbols:
                    if not _should_use_forming_fallback(sym, now_mono):
                        continue
                    bar = _fetch_forming_close(sym)
                    if not bar:
                        row = _symbol_health.get(sym) or {}
                        if row.get("status") not in (_STATUS_LIVE_PUBLIC_TRADE, _STATUS_LIVE_FORMING):
                            _symbol_health[sym] = {
                                "symbol": sym,
                                "status": _STATUS_UNAVAILABLE,
                                "source": "collector_forming",
                                "last_price": row.get("last_price"),
                                "last_update_unix": row.get("last_update_unix"),
                                "age_seconds": row.get("age_seconds"),
                                "alert_count": _count_alerts_for_symbol(sym),
                                "fallback_reason": _fanout_last_error or "forming_null",
                            }
                        continue
                    close = float(bar["close"])
                    st = _STATUS_LIVE_FORMING if sym in _FORMING_ONLY_SYMBOLS or not _fanout_available else _STATUS_LIVE_FORMING
                    _touch_symbol_health(
                        sym,
                        price=close,
                        source="collector_forming",
                        status=st,
                        extra={"forming_time": bar.get("time"), "collector_base": _collector_base_for_symbol(sym)},
                    )
                    _check_symbol_prices(sym, [close], source="collector_forming")

            _refresh_health_ages(symbols)
            time.sleep(0.08)
    finally:
        client.close()


def ensure_monitor_running() -> None:
    global _thread
    with _monitor_lock:
        if _thread is not None and _thread.is_alive():
            return
        _stop_event.clear()
        _thread = threading.Thread(
            target=_monitor_loop,
            name="price_alert_monitor",
            daemon=True,
        )
        _thread.start()


def stop_monitor_for_tests() -> None:
    _stop_event.set()


def get_monitor_status() -> dict[str, Any]:
    """Read-only snapshot for /api/price-alert/monitor-status."""
    symbols = _active_monitor_symbols()
    sym_rows = []
    for sym in sorted(symbols):
        row = dict(_symbol_health.get(sym) or {})
        row.setdefault("symbol", sym)
        row.setdefault("alert_count", _count_alerts_for_symbol(sym))
        sym_rows.append(row)
    return {
        "success": True,
        "fanout_available": _fanout_available,
        "fanout_last_error": _fanout_last_error,
        "fanout_cursor": _fanout_cursor,
        "fanout_coverage_valid": _fanout_coverage_valid,
        "subscriber_ready": _subscriber_ready,
        "active_symbols": sorted(symbols),
        "symbols": sym_rows,
        "forming_only_symbols": sorted(_FORMING_ONLY_SYMBOLS),
    }