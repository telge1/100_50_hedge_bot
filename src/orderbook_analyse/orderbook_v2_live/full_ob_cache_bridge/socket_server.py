"""Unix-domain JSON-lines server for the cache bridge (local-only)."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orjson

logger = logging.getLogger(__name__)


def prepare_bridge_socket(path: Path, *, mode: int = 0o600) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not path.is_socket():
            raise OSError(f"not_a_socket:{path}")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                probe.connect(str(path))
            raise RuntimeError(f"socket_already_active:{path}")
        except (FileNotFoundError, ConnectionRefusedError):
            path.unlink(missing_ok=True)
        except OSError as exc:
            if getattr(exc, "errno", None) in {111, 2}:
                path.unlink(missing_ok=True)
            else:
                raise


class BridgeSocketServer:
    def __init__(
        self,
        path: Path,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        *,
        max_request_bytes: int = 16_384,
        mode: int = 0o600,
        read_timeout_sec: float = 5.0,
    ) -> None:
        self.path = path
        self.handler = handler
        self.max_request_bytes = int(max_request_bytes)
        self.mode = int(mode)
        self.read_timeout_sec = float(read_timeout_sec)
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        prepare_bridge_socket(self.path, mode=self.mode)
        self._server = await asyncio.start_unix_server(self._client, path=str(self.path))
        os.chmod(self.path, self.mode)
        # Verify mode (best-effort; umask may interfere — force again).
        st = self.path.stat()
        if stat.S_IMODE(st.st_mode) != self.mode:
            os.chmod(self.path, self.mode)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            if self.path.is_socket():
                self.path.unlink(missing_ok=True)
        except OSError:
            logger.exception("bridge_socket_unlink_failed")

    async def _client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id = None
        try:
            raw = await asyncio.wait_for(
                reader.readline(), timeout=self.read_timeout_sec
            )
            if not raw:
                return
            if len(raw) > self.max_request_bytes:
                resp = {"ok": False, "error": "request_too_large", "status": "INVALID_REQUEST"}
            else:
                try:
                    req = orjson.loads(raw)
                    if not isinstance(req, dict):
                        raise ValueError("not_object")
                    request_id = req.get("request_id")
                    resp = self.handler(req)
                except Exception as exc:
                    logger.warning("bridge_bad_request %s", type(exc).__name__)
                    resp = {
                        "ok": False,
                        "error": "invalid_json",
                        "status": "INVALID_REQUEST",
                        "request_id": request_id,
                    }
            data = orjson.dumps(resp) + b"\n"
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=self.read_timeout_sec)
        except Exception:
            logger.exception("bridge_client_handler_failed")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
