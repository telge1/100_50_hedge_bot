"""Unix-domain JSON-lines control socket for on-demand OB1000."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 16_384
READ_TIMEOUT_SEC = 5.0
WRITE_TIMEOUT_SEC = 5.0


def default_socket_path() -> Path:
    return Path(f"/run/user/{os.getuid()}/orderbook_ob1000.sock")


def resolve_socket_path(raw: str | None = None) -> Path:
    path_raw = raw or os.environ.get("OB_V3_ON_DEMAND_SOCKET_PATH") or ""
    if path_raw:
        return Path(path_raw)
    return default_socket_path()


def prepare_socket_path(path: Path) -> None:
    parent = path.parent
    if not parent.is_dir():
        raise FileNotFoundError(f"runtime_dir_missing:{parent}")
    if not path.exists():
        return
    if not path.is_socket():
        raise OSError(f"not_a_socket:{path}")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(path))
        raise RuntimeError(f"socket_already_active:{path}")
    except FileNotFoundError:
        path.unlink(missing_ok=True)
    except OSError as exc:
        if getattr(exc, "errno", None) in {111, 2}:
            path.unlink(missing_ok=True)
        elif str(exc).startswith("socket_already_active:"):
            raise
        else:
            raise


def _error_response(request_id: Any, error: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "request_id": request_id,
        "ok": False,
        "error": error,
        "symbol": extra.get("symbol"),
        "depth": extra.get("depth", 1000),
        "subscription_state": extra.get("subscription_state", "error"),
        "expires_at": extra.get("expires_at"),
    }
    out.update({k: v for k, v in extra.items() if k not in out})
    return out


async def _write_response(writer: asyncio.StreamWriter, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    writer.write(data)
    await asyncio.wait_for(writer.drain(), timeout=WRITE_TIMEOUT_SEC)


Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class OnDemandSocketServer:
    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self._handler = handler
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        prepare_socket_path(self.path)
        self._server = await asyncio.start_unix_server(self._accept_client, path=str(self.path))
        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)

    async def stop(self) -> None:
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self.path.is_socket():
            self.path.unlink(missing_ok=True)

    def _accept_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_SEC)
                except asyncio.TimeoutError:
                    break
                if not line:
                    break
                if len(line) > MAX_REQUEST_BYTES:
                    await _write_response(writer, _error_response(None, "request_too_large"))
                    break
                try:
                    req = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    await _write_response(writer, _error_response(None, "invalid_json"))
                    continue
                if not isinstance(req, dict):
                    await _write_response(writer, _error_response(None, "invalid_json"))
                    continue
                try:
                    resp = await self._handler(req)
                except Exception as exc:
                    logger.exception("on_demand_socket_handler_error")
                    resp = _error_response(req.get("request_id"), str(exc))
                await _write_response(writer, resp)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
