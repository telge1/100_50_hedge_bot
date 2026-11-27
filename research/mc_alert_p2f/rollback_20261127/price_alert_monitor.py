"""Central price-alert monitor — reuses collector publicTrade fanout + forming fallback."""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import httpx

from price_alert_store import (
    append_fired_event,
    alert_key,
    condition_met,
    crossed_level,
    load_state,
    save_state,
)
from utils.notifications import play_alert_sound, send_ntfy_alert

logger = logging.getLogger(__name__)

_COLLECTOR_BASE = os.environ.get("STOCH_COLLECTOR_API_BASE", "http://127.0.0.1:8787").rstrip("/")
_QNT_COLLECTOR_BASE = os.environ.get("QNT_COLLECTOR_API_BASE", "http://127.0.0.1:8789").rstrip("/")
_FORMING_SYMBOLS = frozenset({"QNTUSDT"})

_FANOUT_POLL_S = 0.35
_FORMING_POLL_S = 0.9
_TICKER_POLL_S = 2.5
_SUBSCRIBER_ID = "dashboard_price_alert_monitor_v1"

_last_price_by_symbol: dict[str, float] = {}
_initialized_alert_keys: set[str] = set()
_firing_keys: set[str] = set()
_monitor_lock = threading.Lock()
_subscriber_ready = False
_subscriber_cursor: int | None = None
_stop_event = threading.Event()
_thread: threading.Thread | None = None


def _sync_alert_reference_seeds() -> None:
    """Seed last-seen price from alert reference — no retrospective bar high/low."""
    state = load_state()
    live_keys = set(state.keys())
    _initialized_alert_keys.intersection_update(live_keys)
    for key, entry in state.items():
        if not isinstance(entry, dict) or entry.get("engine") != "monitor_v1":
            continue
        if key in _initialized_alert_keys:
            continue
        sym = str(entry.get("symbol") or "").upper()
        ref = entry.get("reference_price")
        if sym and isinstance(ref, (int, float)) and float(ref) > 0:
            _last_price_by_symbol[sym] = float(ref)
        _initialized_alert_keys.add(key)


def _fetch_bybit_last(symbol: str) -> float | None:
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    try:
        url = f"https://api.bybit.com/v5/market/tickers?category=linear&symbol={sym}"
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(url)
        if resp.status_code != 200:
            return None
        data = resp.json()
        items = (data.get("result") or {}).get("list") or []
        if not items:
            return None
        last = items[0].get("lastPrice")
        return float(last) if last is not None else None
    except Exception:
        return None


def _collector_base_for_symbol(symbol: str) -> str:
    sym = str(symbol).upper()
    if sym in _FORMING_SYMBOLS:
        return _QNT_COLLECTOR_BASE
    return _COLLECTOR_BASE


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
        high = float(forming.get("high", close))
        low = float(forming.get("low", close))
        if close <= 0:
            return None
        return {"close": close, "high": high, "low": low, "time": forming.get("time")}
    except Exception:
        return None


def _ensure_fanout_subscriber(client: httpx.Client) -> bool:
    global _subscriber_ready, _subscriber_cursor
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
            _subscriber_cursor = data.get("cursor")
            client.post(
                f"{_COLLECTOR_BASE}/api/trade_fanout/trade_subscriber_heartbeat",
                json={"subscriber_id": _SUBSCRIBER_ID},
            )
            return True
    except Exception as exc:
        logger.debug("fanout subscribe failed: %s", exc)
    return False


def _poll_fanout(client: httpx.Client) -> list[dict[str, Any]]:
    global _subscriber_cursor
    if not _ensure_fanout_subscriber(client):
        return []
    try:
        body: dict[str, Any] = {"subscriber_id": _SUBSCRIBER_ID, "limit": 256}
        if _subscriber_cursor is not None:
            body["cursor"] = _subscriber_cursor
        resp = client.post(f"{_COLLECTOR_BASE}/api/trade_fanout/poll_trade_events", json=body)
        data = resp.json()
        if not data.get("ok"):
            return []
        _subscriber_cursor = data.get("next_cursor", _subscriber_cursor)
        events = data.get("events") or []
        return events if isinstance(events, list) else []
    except Exception:
        return []


def _fire_alert(
    key: str,
    entry: dict[str, Any],
    fired_price: float,
    source: str,
) -> None:
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
    event = append_fired_event(
        alert_key=key,
        symbol=sym,
        target_price=target,
        trigger=trigger,
        fired_price=fired_price,
        source=source,
    )
    msg = f"{sym}: Preis {fired_price} {'≥' if trigger == 'above' else '≤'} {target}"
    tags = ["chart_with_upwards_trend"] if trigger == "above" else ["chart_with_downwards_trend"]
    try:
        send_ntfy_alert(message=msg, title="Price Alert", priority="high", tags=tags)
        play_alert_sound(repeats=2)
    except Exception as exc:
        logger.warning("price alert notify failed: %s", exc)
    finally:
        with _monitor_lock:
            _firing_keys.discard(key)
    logger.info("price alert fired %s @ %s (%s)", key, fired_price, source)
    _last_price_by_symbol[sym] = fired_price


def _check_symbol_prices(
    symbol: str,
    prices: list[float],
    *,
    source: str,
    armed_after_unix: int | None = None,
) -> None:
    sym = symbol.upper()
    state = load_state()
    if not state:
        return
    prev0 = _last_price_by_symbol.get(sym)
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
    last_ticker: dict[str, float] = {}
    client = httpx.Client(timeout=3.0)
    try:
        while not _stop_event.is_set():
            symbols = _active_monitor_symbols()
            if not symbols:
                time.sleep(0.5)
                continue
            _sync_alert_reference_seeds()

            now = time.monotonic()

            if now - last_ticker.get("_fanout", 0) >= _FANOUT_POLL_S:
                last_ticker["_fanout"] = now
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
                    _check_symbol_prices(sym, [px], source="public_trade_fanout")

            forming_due = now - last_forming.get("_tick", 0) >= _FORMING_POLL_S
            if forming_due:
                last_forming["_tick"] = now
                for sym in symbols:
                    if sym in _FORMING_SYMBOLS or sym not in _last_price_by_symbol:
                        bar = _fetch_forming_close(sym)
                        if not bar:
                            continue
                        # Close only: avoid retrospective touch via partial-bar high/low.
                        _check_symbol_prices(sym, [float(bar["close"])], source="collector_forming")

            ticker_due = now - last_ticker.get("_tick", 0) >= _TICKER_POLL_S
            if ticker_due:
                last_ticker["_tick"] = now
                for sym in symbols:
                    if sym in _FORMING_SYMBOLS:
                        continue
                    need_ticker = sym == "BTCUSDT" or sym not in _last_price_by_symbol
                    if not need_ticker:
                        continue
                    px = _fetch_bybit_last(sym)
                    if px is not None:
                        _check_symbol_prices(sym, [px], source="bybit_ticker_fallback")

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