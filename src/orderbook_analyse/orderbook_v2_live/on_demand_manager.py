"""On-demand OB1000 manager — reuses collector WS subscribe/resync path."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from orderbook_analyse.orderbook_v2_live.clock import LiveSecondClock, SequenceBreak
from orderbook_analyse.orderbook_v2_live.depth import orderbook_topic, parse_orderbook_topic
from orderbook_analyse.orderbook_v2_live.on_demand_lease import (
    ON_DEMAND_DEPTH as LEASE_DEPTH,
    LeaseKey,
    LeaseManager,
    PILOT_SYMBOLS,
)
from orderbook_analyse.orderbook_v2_live.on_demand_snapshot import build_snapshot_payload
from orderbook_analyse.orderbook_v2_live.on_demand_socket import resolve_socket_path

logger = logging.getLogger(__name__)

VALID_OPERATIONS = frozenset({"acquire", "heartbeat", "release", "snapshot", "status"})


@dataclass
class OnDemandRuntime:
    symbol: str
    depth: int
    clock: LiveSecondClock
    subscribed: bool = False
    subscription_confirmed: bool = False
    dropping_until_subscribe_ack: bool = False
    active_generation: int | None = None
    pending_raw: list[tuple[dict[str, Any], datetime]] = field(default_factory=list)
    subscription_state: str = "stopped"
    last_event_timestamp: datetime | None = None
    last_error: str = ""


def load_on_demand_settings() -> dict[str, Any]:
    enabled = (os.environ.get("OB_V3_ON_DEMAND_ENABLE") or "false").lower() in {"1", "true", "yes"}
    return {
        "enabled": enabled,
        "max_active_topics": int(os.environ.get("OB_V3_ON_DEMAND_MAX_ACTIVE") or "4"),
        "heartbeat_sec": float(os.environ.get("OB_V3_ON_DEMAND_HEARTBEAT_SEC") or "15"),
        "lease_ttl_sec": float(os.environ.get("OB_V3_ON_DEMAND_LEASE_TTL_SEC") or "45"),
        "socket_path": resolve_socket_path(),
        "pilot_symbols": PILOT_SYMBOLS,
    }


class OnDemandDepthManager:
    """Optional OB1000 on-demand layer. Disabled by default; zero impact on OB200."""

    def __init__(
        self,
        *,
        exchange: str,
        market: str,
        send_chunk: Callable,
        confirmed_topics: list[str],
        settings: dict[str, Any] | None = None,
    ) -> None:
        cfg = settings or load_on_demand_settings()
        self.enabled = bool(cfg["enabled"])
        self.exchange = exchange
        self.market = market
        self._send_chunk = send_chunk
        self._confirmed_topics = confirmed_topics
        self.socket_path: Path = Path(cfg["socket_path"])
        self.leases = LeaseManager(
            heartbeat_sec=cfg["heartbeat_sec"],
            lease_ttl_sec=cfg["lease_ttl_sec"],
            max_active_topics=cfg["max_active_topics"],
            pilot_symbols=cfg["pilot_symbols"],
        )
        self.runtimes: dict[tuple[str, int], OnDemandRuntime] = {}
        self._inflight_subscribe: set[tuple[str, int]] = set()
        self._book_lock = threading.Lock()
        self._grace_until: dict[LeaseKey, datetime] = {}

    def topic_map(self) -> dict[str, tuple[str, int]]:
        return {
            orderbook_topic(sym, depth): (sym, depth)
            for sym, depth in ((k.symbol, k.depth) for k in self.leases.active_keys())
        }

    def _get_runtime(self, symbol: str, depth: int) -> OnDemandRuntime:
        key = (symbol.upper(), depth)
        rt = self.runtimes.get(key)
        if rt is None:
            rt = OnDemandRuntime(
                symbol=symbol.upper(),
                depth=depth,
                clock=LiveSecondClock(
                    symbol=symbol.upper(),
                    depth=depth,
                    exchange=self.exchange,
                    market=self.market,
                ),
            )
            self.runtimes[key] = rt
        return rt

    def _arm_runtime(self, rt: OnDemandRuntime) -> None:
        rt.dropping_until_subscribe_ack = False
        rt.active_generation = rt.clock.generation
        pending = rt.pending_raw
        rt.pending_raw = []
        for payload, received_at in pending:
            self._ingest(rt, payload, received_at)

    def handle_message(self, payload: dict[str, Any], received_at: datetime) -> bool:
        if not self.enabled:
            return False
        parsed = parse_orderbook_topic(str(payload.get("topic") or ""))
        if parsed is None:
            return False
        symbol, depth = parsed
        if depth != LEASE_DEPTH:
            return False
        key = LeaseKey(symbol, depth)
        if self.leases.active_count(key) <= 0:
            return False
        rt = self._get_runtime(symbol, depth)
        if rt.dropping_until_subscribe_ack or rt.active_generation is None:
            rt.pending_raw.append((payload, received_at))
            return True
        self._ingest(rt, payload, received_at)
        return True

    def _ingest(self, rt: OnDemandRuntime, payload: dict[str, Any], received_at: datetime) -> None:
        msg_type = str(payload.get("type") or "")
        ts_ms = int(payload.get("ts") or 0)
        data = payload.get("data") or {}
        if str(data.get("s") or rt.symbol) != rt.symbol:
            rt.clock.stats.dropped_events += 1
            return
        rt.last_event_timestamp = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
        with self._book_lock:
            try:
                rt.clock.ingest(msg_type, ts_ms, data, generation=rt.active_generation)
            except SequenceBreak as exc:
                rt.clock.begin_resync()
                rt.dropping_until_subscribe_ack = True
                rt.active_generation = None
                rt.subscription_state = "starting"
                rt.last_error = str(exc)
                logger.warning("on_demand_resync %s depth=%s reason=%s", rt.symbol, rt.depth, exc)
                return
            if rt.clock.last_valid_book and rt.clock.last_valid_book.is_valid:
                rt.subscription_state = "live"

    def _runtime_state(self, symbol: str, depth: int = LEASE_DEPTH) -> str:
        key = LeaseKey(symbol.upper(), depth)
        grace_until = self._grace_until.get(key)
        if grace_until is not None:
            if datetime.now(timezone.utc) < grace_until:
                return "grace"
            return "stopped"
        rt = self.runtimes.get((symbol.upper(), depth))
        if rt is None:
            return "stopped"
        return rt.subscription_state

    def _lease_expires_at(self, lease_id: str) -> str | None:
        lease = self.leases._leases.get(lease_id)
        if lease is None:
            return None
        return lease.expires_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    def _base_response(
        self,
        req: dict[str, Any],
        *,
        ok: bool,
        error: str | None,
        symbol: str | None = None,
        depth: int = LEASE_DEPTH,
        subscription_state: str = "stopped",
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        return {
            "request_id": req.get("request_id"),
            "ok": ok,
            "error": error,
            "symbol": symbol,
            "depth": depth,
            "subscription_state": subscription_state,
            "expires_at": expires_at,
        }

    async def handle_request(self, req: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            return self._base_response(req, ok=False, error="disabled", subscription_state="stopped")
        op = str(req.get("operation") or "").strip().lower()
        if op not in VALID_OPERATIONS:
            return self._base_response(req, ok=False, error="unknown_operation", subscription_state="error")
        depth_raw = req.get("depth", LEASE_DEPTH)
        try:
            depth = int(depth_raw)
        except (TypeError, ValueError):
            return self._base_response(req, ok=False, error="invalid_depth", subscription_state="error")
        if depth != LEASE_DEPTH:
            return self._base_response(
                req, ok=False, error="only_depth_1000_supported", depth=depth, subscription_state="error"
            )
        symbol = str(req.get("symbol") or "").strip().upper()
        lease_id = str(req.get("lease_id") or "").strip()
        try:
            if op == "acquire":
                if not symbol:
                    raise ValueError("symbol_required")
                if not lease_id:
                    raise ValueError("lease_id_required")
                lease, _subscribe = self.leases.acquire(
                    symbol=symbol,
                    session_id=lease_id,
                    lease_id=lease_id,
                )
                state = self._runtime_state(lease.symbol, lease.depth)
                if state == "stopped":
                    state = "starting"
                return self._base_response(
                    req,
                    ok=True,
                    error=None,
                    symbol=lease.symbol,
                    depth=lease.depth,
                    subscription_state=state,
                    expires_at=self._lease_expires_at(lease.lease_id),
                )
            if op == "heartbeat":
                if not lease_id:
                    raise ValueError("lease_id_required")
                lease = self.leases.heartbeat(
                    lease_id,
                    symbol=symbol or None,
                    depth=depth,
                )
                state = self._runtime_state(lease.symbol, lease.depth)
                return self._base_response(
                    req,
                    ok=True,
                    error=None,
                    symbol=lease.symbol,
                    depth=lease.depth,
                    subscription_state=state,
                    expires_at=self._lease_expires_at(lease.lease_id),
                )
            if op == "release":
                if not lease_id:
                    raise ValueError("lease_id_required")
                lease = self.leases._leases.get(lease_id)
                sym = lease.symbol if lease is not None else (symbol or None)
                dep = lease.depth if lease is not None else depth
                key, unsub = self.leases.release(lease_id)
                if key is not None and unsub:
                    self._grace_until[key] = datetime.now(timezone.utc) + timedelta(
                        seconds=self.leases.lease_ttl_sec
                    )
                return self._base_response(
                    req,
                    ok=True,
                    error=None,
                    symbol=sym,
                    depth=dep,
                    subscription_state="grace" if key is not None and unsub else "stopped",
                    expires_at=None,
                )
            if op == "status":
                sym = symbol or None
                state = self._runtime_state(sym, depth) if sym else "stopped"
                if sym and self.leases.active_count(LeaseKey(sym, depth)) <= 0:
                    state = "stopped"
                return self._base_response(
                    req,
                    ok=True,
                    error=None,
                    symbol=sym,
                    depth=depth,
                    subscription_state=state,
                    expires_at=self._lease_expires_at(lease_id) if lease_id else None,
                )
            if op == "snapshot":
                if not symbol:
                    raise ValueError("symbol_required")
                if self.leases.active_count(LeaseKey(symbol, depth)) <= 0:
                    return self._base_response(
                        req,
                        ok=False,
                        error="no_active_lease",
                        symbol=symbol,
                        depth=depth,
                        subscription_state="stopped",
                    )
                snap = self.build_snapshot(symbol)
                resp = self._base_response(
                    req,
                    ok=True,
                    error=None,
                    symbol=symbol,
                    depth=depth,
                    subscription_state=snap.get("subscription_state", "unknown"),
                    expires_at=self._lease_expires_at(lease_id) if lease_id else None,
                )
                resp.update(snap)
                return resp
        except RuntimeError as exc:
            if str(exc) == "capacity_reached":
                return self._base_response(
                    req,
                    ok=False,
                    error="capacity_reached",
                    symbol=symbol or None,
                    depth=depth,
                    subscription_state="capacity",
                )
            return self._base_response(
                req, ok=False, error=str(exc), symbol=symbol or None, depth=depth, subscription_state="error"
            )
        except KeyError:
            return self._base_response(
                req,
                ok=False,
                error="unknown_lease",
                symbol=symbol or None,
                depth=depth,
                subscription_state="stopped",
            )
        except ValueError as exc:
            return self._base_response(
                req,
                ok=False,
                error=str(exc),
                symbol=symbol or None,
                depth=depth,
                subscription_state="error",
            )
        raise AssertionError(f"unhandled op {op}")

    def build_snapshot(self, symbol: str) -> dict[str, Any]:
        sym = str(symbol).upper()
        rt = self.runtimes.get((sym, LEASE_DEPTH))
        if rt is None:
            return {
                "timestamp_utc": None,
                "source": "orderbook_v3_live_on_demand",
                "coverage": "on_demand",
                "freshness_state": "unknown",
                "bids": [],
                "asks": [],
                "data_status": "no_data",
            }
        with self._book_lock:
            book = rt.clock.last_valid_book
            if book is None or not book.is_valid:
                return {
                    "timestamp_utc": None,
                    "source": "orderbook_v3_live_on_demand",
                    "coverage": "on_demand",
                    "freshness_state": "unknown",
                    "bids": [],
                    "asks": [],
                    "subscription_state": rt.subscription_state,
                    "data_status": "no_data",
                }
            if rt.last_event_timestamp is None:
                return {
                    "timestamp_utc": None,
                    "source": "orderbook_v3_live_on_demand",
                    "coverage": "on_demand",
                    "freshness_state": "unknown",
                    "bids": [],
                    "asks": [],
                    "subscription_state": rt.subscription_state,
                    "data_status": "no_data",
                }
            now = datetime.now(timezone.utc)
            freshness_ms = max(0, int((now - rt.last_event_timestamp).total_seconds() * 1000))
            if freshness_ms <= 15_000:
                freshness_state = "fresh"
            elif freshness_ms <= 180_000:
                freshness_state = "delayed"
            else:
                freshness_state = "stale"
            return build_snapshot_payload(
                symbol=sym,
                depth=LEASE_DEPTH,
                book=book,
                timestamp_utc=rt.last_event_timestamp,
                subscription_state=rt.subscription_state,
                freshness_state=freshness_state,
                freshness_ms=freshness_ms,
            )

    async def subscribe_key(self, ws, key: LeaseKey) -> None:
        topic = orderbook_topic(key.symbol, key.depth)
        rt = self._get_runtime(key.symbol, key.depth)
        if topic in self._confirmed_topics and rt.subscription_confirmed:
            rt.subscription_state = "live" if rt.clock.last_valid_book else "starting"
            return
        if (key.symbol, key.depth) in self._inflight_subscribe:
            return
        self._inflight_subscribe.add((key.symbol, key.depth))
        try:
            rt.clock.begin_resync()
            rt.dropping_until_subscribe_ack = True
            rt.active_generation = None
            rt.subscription_state = "starting"
            rt.subscribed = True
            rt.subscription_confirmed = False
            await self._send_chunk(ws, "subscribe", [topic])
            if topic not in self._confirmed_topics:
                self._confirmed_topics.append(topic)
            rt.subscription_confirmed = True
            self._arm_runtime(rt)
            if rt.clock.last_valid_book and rt.clock.last_valid_book.is_valid:
                rt.subscription_state = "live"
        finally:
            self._inflight_subscribe.discard((key.symbol, key.depth))

    async def unsubscribe_key(self, ws, key: LeaseKey) -> None:
        topic = orderbook_topic(key.symbol, key.depth)
        try:
            if topic in self._confirmed_topics:
                await self._send_chunk(ws, "unsubscribe", [topic])
                self._confirmed_topics.remove(topic)
        finally:
            self.runtimes.pop((key.symbol, key.depth), None)

    def on_reconnect(self) -> None:
        now = datetime.now(timezone.utc)
        expired = self.leases.expire_due(now=now)
        for key, _unsub in expired:
            self._grace_until.pop(key, None)
            self.runtimes.pop((key.symbol, key.depth), None)
        self._grace_until = {
            key: until for key, until in self._grace_until.items() if until > now
        }
        for key in list(self.leases.active_keys()):
            topic = orderbook_topic(key.symbol, key.depth)
            if topic in self._confirmed_topics:
                self._confirmed_topics.remove(topic)
            rt = self.runtimes.get((key.symbol, key.depth))
            if rt is not None:
                rt.subscription_confirmed = False
                rt.dropping_until_subscribe_ack = True
                rt.active_generation = None
                rt.subscription_state = "starting"
                rt.pending_raw.clear()

    async def tick(self, ws) -> None:
        if not self.enabled:
            return
        now = datetime.now(timezone.utc)
        expired = self.leases.expire_due(now=now)
        for key, unsub in expired:
            if unsub:
                self._grace_until[key] = now + timedelta(seconds=self.leases.lease_ttl_sec)
        for key, until in list(self._grace_until.items()):
            if now >= until:
                del self._grace_until[key]
                await self.unsubscribe_key(ws, key)
        for key in self.leases.active_keys():
            if (key.symbol, key.depth) not in self._inflight_subscribe:
                await self.subscribe_key(ws, key)

    def health_dict(self) -> dict[str, Any]:
        if not self.enabled:
            return {"on_demand_enabled": False}
        return {
            "on_demand_enabled": True,
            "on_demand_socket_path": str(self.socket_path),
            "on_demand_active_topics": self.leases.active_topic_count(),
            "on_demand_leases": self.leases.lease_summary(),
            "on_demand_runtimes": [
                {
                    "symbol": rt.symbol,
                    "depth": rt.depth,
                    "subscription_state": rt.subscription_state,
                    "book_valid": bool(rt.clock.last_valid_book and rt.clock.last_valid_book.is_valid),
                    "last_error": rt.last_error,
                }
                for rt in self.runtimes.values()
            ],
        }
