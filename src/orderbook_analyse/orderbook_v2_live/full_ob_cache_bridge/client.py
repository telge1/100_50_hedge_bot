"""Read-only client for full_ob_cache_bridge_protocol_v1."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import orjson

from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.freeze import (
    canonical_manifest_hash,
    sha256_bytes,
)
from orderbook_analyse.orderbook_v2_live.full_ob_cache_bridge.protocol import (
    KNOWN_STATUS_CODES,
    OP_FREEZE_PRE_ROLL,
    OP_STATUS,
    PROTOCOL_VERSION,
    STATUS_INTERNAL_ERROR,
    STATUS_PROTOCOL_MISMATCH,
)


class BridgeClientError(RuntimeError):
    def __init__(self, message: str, *, status: str = STATUS_INTERNAL_ERROR) -> None:
        super().__init__(message)
        self.status = status


class FullObCacheBridgeClient:
    def __init__(
        self,
        socket_path: str | Path,
        *,
        timeout_sec: float = 10.0,
        max_response_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout_sec = float(timeout_sec)
        self.max_response_bytes = int(max_response_bytes)

    def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = orjson.dumps(payload) + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout_sec)
            sock.connect(str(self.socket_path))
            sock.sendall(raw)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > self.max_response_bytes:
                    raise BridgeClientError("response_too_large")
        if not buf.strip():
            raise BridgeClientError("empty_response")
        line = buf.split(b"\n", 1)[0]
        try:
            obj = orjson.loads(line)
        except Exception as exc:
            raise BridgeClientError("invalid_response_json") from exc
        if not isinstance(obj, dict):
            raise BridgeClientError("invalid_response_type")
        # Unknown fields ignored; protocol version checked when present.
        pv = obj.get("protocol_version")
        if pv is not None and pv != PROTOCOL_VERSION:
            raise BridgeClientError("protocol_mismatch", status=STATUS_PROTOCOL_MISMATCH)
        status = obj.get("status")
        if status is not None and status not in KNOWN_STATUS_CODES:
            raise BridgeClientError(f"unknown_status:{status}", status=STATUS_INTERNAL_ERROR)
        return obj

    def status(self) -> dict[str, Any]:
        return self._request(
            {
                "protocol_version": PROTOCOL_VERSION,
                "operation": OP_STATUS,
            }
        )

    def freeze_pre_roll(
        self,
        *,
        request_id: str,
        symbol: str,
        requested_pre_roll_seconds: float,
        client_request_time: str | None = None,
    ) -> dict[str, Any]:
        req: dict[str, Any] = {
            "protocol_version": PROTOCOL_VERSION,
            "operation": OP_FREEZE_PRE_ROLL,
            "request_id": request_id,
            "symbol": symbol,
            "requested_pre_roll_seconds": float(requested_pre_roll_seconds),
        }
        if client_request_time is not None:
            req["client_request_time"] = client_request_time
        return self._request(req)

    @staticmethod
    def verify_manifest(manifest: dict[str, Any]) -> bool:
        expected = canonical_manifest_hash(manifest)
        return expected == str(manifest.get("manifest_sha256") or "")

    @staticmethod
    def verify_payload_hash(payload_bytes: bytes, expected_sha256: str) -> bool:
        return sha256_bytes(payload_bytes) == str(expected_sha256 or "")
