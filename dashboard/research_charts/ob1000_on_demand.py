"""Dashboard bridge to on-demand OB1000 collector via Unix socket."""

from __future__ import annotations

import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PILOT_SYMBOLS = frozenset({"BTCUSDT", "DOGEUSDT"})
ON_DEMAND_DEPTH = 1000
SOURCE_NAME = "orderbook_v3_live_on_demand"

CONNECT_TIMEOUT_SEC = 2.0
READ_TIMEOUT_SEC = 3.0
WRITE_TIMEOUT_SEC = 3.0
MAX_RESPONSE_BYTES = 8_388_608


class Ob1000DisabledError(RuntimeError):
    pass


class Ob1000CollectorUnavailableError(RuntimeError):
    pass


class Ob1000CapacityError(RuntimeError):
    pass


class Ob1000RequestError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


def _on_demand_enabled() -> bool:
    return (os.environ.get("OB_V3_ON_DEMAND_ENABLE") or "false").lower() in {"1", "true", "yes"}


def socket_path() -> Path:
    raw = os.environ.get("OB_V3_ON_DEMAND_SOCKET_PATH") or ""
    if raw:
        return Path(raw)
    try:
        uid = os.getuid()
    except AttributeError:
        uid = 1000
    return Path(f"/run/user/{uid}/orderbook_ob1000.sock")


def _call_collector(request: dict[str, Any]) -> dict[str, Any]:
    if not _on_demand_enabled():
        raise Ob1000DisabledError("disabled")
    path = socket_path()
    if not path.is_socket():
        raise Ob1000CollectorUnavailableError("collector_unavailable")
    payload = dict(request)
    payload.setdefault("depth", ON_DEMAND_DEPTH)
    data = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(CONNECT_TIMEOUT_SEC)
        try:
            sock.connect(str(path))
        except (FileNotFoundError, PermissionError, OSError) as exc:
            raise Ob1000CollectorUnavailableError("collector_unavailable") from exc
        sock.settimeout(READ_TIMEOUT_SEC)
        sock.sendall(data)
        chunks: list[bytes] = []
        total = 0
        while True:
            part = sock.recv(65536)
            if not part:
                break
            total += len(part)
            if total > MAX_RESPONSE_BYTES:
                raise Ob1000RequestError("response_too_large")
            chunks.append(part)
            if b"\n" in part:
                break
    raw = b"".join(chunks).split(b"\n", 1)[0]
    try:
        resp = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Ob1000RequestError("invalid_collector_response") from exc
    if not isinstance(resp, dict):
        raise Ob1000RequestError("invalid_collector_response")
    return resp


def _normalize_symbol(symbol: str) -> str:
    sym = str(symbol or "").strip().upper()
    if sym not in PILOT_SYMBOLS:
        raise ValueError("symbol_not_in_pilot")
    return sym


def _map_collector_error(resp: dict[str, Any]) -> None:
    err = str(resp.get("error") or "")
    if err == "disabled":
        raise Ob1000DisabledError(err)
    if err == "capacity_reached":
        raise Ob1000CapacityError(err)
    if err:
        raise Ob1000RequestError(err, err)


def lease_acquire(*, symbol: str, session_id: str, lease_id: str | None = None) -> dict[str, Any]:
    sym = _normalize_symbol(symbol)
    lid = lease_id or str(uuid.uuid4())
    resp = _call_collector(
        {
            "request_id": str(uuid.uuid4()),
            "operation": "acquire",
            "lease_id": lid,
            "symbol": sym,
            "depth": ON_DEMAND_DEPTH,
        }
    )
    if not resp.get("ok"):
        _map_collector_error(resp)
    return {
        "lease_id": lid,
        "symbol": resp.get("symbol") or sym,
        "depth": ON_DEMAND_DEPTH,
        "subscription_state": resp.get("subscription_state") or "starting",
        "expires_at": resp.get("expires_at"),
        "status": resp.get("subscription_state") or "starting",
    }


def lease_heartbeat(*, lease_id: str, symbol: str | None = None) -> dict[str, Any]:
    req: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "operation": "heartbeat",
        "lease_id": str(lease_id),
        "depth": ON_DEMAND_DEPTH,
    }
    if symbol:
        req["symbol"] = _normalize_symbol(symbol)
    resp = _call_collector(req)
    if not resp.get("ok"):
        _map_collector_error(resp)
    return {
        "lease_id": lease_id,
        "symbol": resp.get("symbol"),
        "depth": ON_DEMAND_DEPTH,
        "subscription_state": resp.get("subscription_state"),
        "expires_at": resp.get("expires_at"),
        "status": resp.get("subscription_state") or "heartbeat_ok",
    }


def lease_release(*, lease_id: str) -> dict[str, Any]:
    resp = _call_collector(
        {
            "request_id": str(uuid.uuid4()),
            "operation": "release",
            "lease_id": str(lease_id),
            "depth": ON_DEMAND_DEPTH,
        }
    )
    if not resp.get("ok"):
        _map_collector_error(resp)
    return {
        "lease_id": lease_id,
        "status": "released",
        "subscription_state": resp.get("subscription_state") or "stopped",
    }


def load_ob1000_levels(symbol: str, *, lease_id: str | None = None) -> dict[str, Any]:
    sym = _normalize_symbol(symbol)
    req: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "operation": "snapshot",
        "symbol": sym,
        "depth": ON_DEMAND_DEPTH,
    }
    if lease_id:
        req["lease_id"] = str(lease_id)
    resp = _call_collector(req)
    if not resp.get("ok"):
        err = str(resp.get("error") or "")
        if err in {"no_active_lease", "unknown_lease"}:
            return {
                "symbol": sym,
                "depth": ON_DEMAND_DEPTH,
                "source": SOURCE_NAME,
                "subscription_state": resp.get("subscription_state") or "stopped",
                "freshness_state": "unknown",
                "freshness_ms": None,
                "timestamp_utc": None,
                "sequence": None,
                "bids": [],
                "asks": [],
                "data_status": "no_data",
                "coverage": "on_demand",
            }
        _map_collector_error(resp)
    payload = dict(resp)
    payload.setdefault("symbol", sym)
    payload.setdefault("depth", ON_DEMAND_DEPTH)
    payload.setdefault("source", SOURCE_NAME)
    payload.setdefault("coverage", "on_demand")
    return payload


def freshness_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    ts = payload.get("timestamp_utc")
    if not ts:
        if payload.get("freshness_state") is None:
            payload["freshness_state"] = "unknown"
        payload["freshness_ms"] = payload.get("freshness_ms")
        return payload
    try:
        if str(ts).endswith("Z"):
            book_ts = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        else:
            book_ts = datetime.fromisoformat(str(ts))
            if book_ts.tzinfo is None:
                book_ts = book_ts.replace(tzinfo=timezone.utc)
    except ValueError:
        payload["freshness_state"] = "unknown"
        payload["freshness_ms"] = None
        return payload
    now = datetime.now(timezone.utc)
    ms = int((now - book_ts).total_seconds() * 1000)
    payload["freshness_ms"] = ms
    if ms < 0:
        payload["freshness_state"] = "unknown"
    elif ms <= 15_000:
        payload["freshness_state"] = "fresh"
    elif ms <= 180_000:
        payload["freshness_state"] = "delayed"
    else:
        payload["freshness_state"] = "stale"
    return payload
