"""Unix-socket client for Full-OB event fanout + case archive (local only)."""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def default_socket_path() -> Path:
    return Path(f"/run/user/{os.getuid()}/orderbook_ob1000.sock")


@dataclass
class FanoutClient:
    """JSON request/response over AF_UNIX. Fail-closed on unknown ops/cursors."""

    socket_path: Path = field(default_factory=default_socket_path)
    timeout_sec: float = 5.0

    def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        req = {
            "request_id": fields.pop("request_id", f"req-{uuid.uuid4().hex[:10]}"),
            "operation": operation,
            **fields,
        }
        payload = (json.dumps(req, separators=(",", ":")) + "\n").encode("utf-8")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout_sec)
            sock.connect(str(self.socket_path))
            sock.sendall(payload)
            chunks: list[bytes] = []
            while True:
                data = sock.recv(65536)
                if not data:
                    break
                chunks.append(data)
                if b"\n" in data:
                    break
        raw = b"".join(chunks).decode("utf-8").strip()
        if not raw:
            return {"ok": False, "error": "empty_response"}
        try:
            return json.loads(raw.splitlines()[0])
        except json.JSONDecodeError:
            return {"ok": False, "error": "invalid_json_response"}

    def create_subscriber(self, *, symbol: str, max_queue: int | None = None) -> dict[str, Any]:
        return self.request("create_subscriber", symbol=symbol, max_queue=max_queue)

    def poll_events(
        self, *, subscriber_id: str, cursor: int | None = None, limit: int = 256
    ) -> dict[str, Any]:
        return self.request(
            "poll_events", subscriber_id=subscriber_id, cursor=cursor, limit=limit
        )

    def subscriber_heartbeat(self, *, subscriber_id: str) -> dict[str, Any]:
        return self.request("subscriber_heartbeat", subscriber_id=subscriber_id)

    def remove_subscriber(self, *, subscriber_id: str) -> dict[str, Any]:
        return self.request("remove_subscriber", subscriber_id=subscriber_id)

    def start_case_archive(self, **fields: Any) -> dict[str, Any]:
        return self.request("start_case_archive", **fields)

    def finalize_case_archive(self, *, recorder_id: str) -> dict[str, Any]:
        return self.request("finalize_case_archive", recorder_id=recorder_id)

    def case_archive_status(self, *, recorder_id: str) -> dict[str, Any]:
        return self.request("case_archive_status", recorder_id=recorder_id)


@dataclass
class InProcessFanoutBridge:
    """Direct bridge to FullObEventFanout / CaseArchive for isolated smoke (no WS)."""

    fanout: Any
    case_archive: Any | None = None

    def request(self, operation: str, **fields: Any) -> dict[str, Any]:
        req = {"request_id": f"local-{uuid.uuid4().hex[:8]}", "operation": operation, **fields}
        for hub in (self.fanout, self.case_archive):
            if hub is None:
                continue
            resp = hub.handle_request(req)
            if resp is not None:
                return resp
        return {"ok": False, "error": "unknown_operation", "operation": operation}

    def create_subscriber(self, *, symbol: str, max_queue: int | None = None) -> dict[str, Any]:
        return self.request("create_subscriber", symbol=symbol, max_queue=max_queue)

    def poll_events(
        self, *, subscriber_id: str, cursor: int | None = None, limit: int = 256
    ) -> dict[str, Any]:
        return self.request(
            "poll_events", subscriber_id=subscriber_id, cursor=cursor, limit=limit
        )

    def subscriber_heartbeat(self, *, subscriber_id: str) -> dict[str, Any]:
        return self.request("subscriber_heartbeat", subscriber_id=subscriber_id)

    def remove_subscriber(self, *, subscriber_id: str) -> dict[str, Any]:
        return self.request("remove_subscriber", subscriber_id=subscriber_id)

    def start_case_archive(self, **fields: Any) -> dict[str, Any]:
        return self.request("start_case_archive", **fields)

    def finalize_case_archive(self, *, recorder_id: str) -> dict[str, Any]:
        return self.request("finalize_case_archive", recorder_id=recorder_id)

    def case_archive_status(self, *, recorder_id: str) -> dict[str, Any]:
        return self.request("case_archive_status", recorder_id=recorder_id)
